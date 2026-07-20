"""Local ecash proxy — a private "swap the API" endpoint.

Runs an OpenAI-compatible server on your own machine that pays for each request
with blind-signed ecash from your wallet, then forwards to the hosted router.
Point any agent/tool (Cursor, aider, the OpenAI SDK, ...) at
`http://localhost:<port>/v1` with no code changes, and every request becomes
private, unlinkable pay-per-use inference — the router can't tie your tool's
requests to each other or to your deposit.

Stdlib only (no FastAPI/uvicorn), so it ships in the slim CLI install. Single
user, localhost: a lock serializes ecash spends so concurrent requests from a
tool can't race the wallet.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# Bare OpenAI/Anthropic model names an agent is likely to send, mapped to the
# provider-prefixed IDs the router/OpenRouter expects. Unknown names pass through.
_ALIASES = {
    "gpt-4o": "openai/gpt-4o", "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4.1": "openai/gpt-4.1", "gpt-4.1-mini": "openai/gpt-4.1-mini",
    "gpt-4-turbo": "openai/gpt-4-turbo", "gpt-3.5-turbo": "openai/gpt-3.5-turbo",
    "claude-3.5-sonnet": "anthropic/claude-3.5-sonnet",
    "claude-3-5-sonnet": "anthropic/claude-3.5-sonnet",
    "claude-3.5-haiku": "anthropic/claude-3.5-haiku",
    "claude-3-opus": "anthropic/claude-3-opus",
}


def _map_model(m: str) -> str:
    """Accept bare model names from agents; map to provider-prefixed router IDs."""
    if not m or "/" in m:
        return m
    if m in _ALIASES:
        return _ALIASES[m]
    if m.startswith(("gpt", "o1", "o3", "o4")):
        return "openai/" + m
    if m.startswith("claude"):
        return "anthropic/" + m
    return m


def run_proxy(wallet, host: str, port: int, daemon_key: str = "",
              default_model: str = "openai/gpt-4o-mini") -> None:
    import os
    lock = threading.Lock()
    # Balance-less funding (privacy): we do NOT claim ecash on-demand right before
    # a spend — that just-in-time claim is a deterministic claim->spend marker the
    # router can use to re-link usage to the funded account. Instead we drain the
    # ENTIRE account balance into ecash ONCE, here at startup, decoupled from any
    # individual request. When ecash runs out the user funds + claims again (a
    # deliberate event, not one triggered by a spend).
    def _claim_all_at_startup():
        try:
            if wallet.account and wallet.account_status().get("balance", 0) > 0:
                wallet.claim_all()
        except Exception:
            pass  # best-effort; a truly empty wallet still returns a clear 402

    import time as _time
    _models = {"set": None, "ts": 0.0}

    def _available_models():
        if _models["set"] is None or _time.time() - _models["ts"] > 600:
            try:
                r = wallet.http.get(f"{wallet.url}/v1/models", timeout=10)
                _models["set"] = {m["id"] for m in r.json().get("data", [])}
                _models["ts"] = _time.time()
            except Exception:
                pass
        return _models["set"] or set()

    # First-run onboarding: make sure there's an account to fund.
    if not wallet.account and wallet.balance() == 0:
        try:
            wallet.new_account()
        except Exception:
            pass
    # Drain any already-funded account balance into ecash once, up front.
    _claim_all_at_startup()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if not daemon_key:
                return True
            return self.headers.get("Authorization", "") == f"Bearer {daemon_key}"

        def do_GET(self):
            self.path = self.path.split("?", 1)[0]  # drop query (Claude Code sends ?beta=true)
            if self.path.rstrip("/") == "/healthz":
                self._json(200, {"ok": True, "balance": wallet.balance(),
                                 "router": wallet.url})
            elif self.path.rstrip("/").endswith("/models"):
                try:
                    r = wallet.http.get(f"{wallet.url}/v1/models")
                    self._json(r.status_code, r.json())
                except Exception as e:
                    self._json(502, {"error": str(e)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0].rstrip("/")  # drop query (?beta=true)
            is_msgs = path.endswith("/messages")          # Anthropic (Claude Code)
            is_chat = path.endswith("/chat/completions")  # OpenAI
            if not (is_msgs or is_chat):
                return self._json(404, {"error": "not found"})
            if not self._authed():
                return self._json(401, {"error": "missing or invalid daemon key"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json(400, {"error": "invalid JSON body"})
            if is_msgs:
                return self._messages(body)
            messages = body.get("messages")
            if not messages:
                return self._json(400, {"error": "messages required"})
            model = _map_model(body.get("model") or default_model)
            want_stream = bool(body.get("stream"))
            kwargs = {k: v for k, v in body.items()
                      if k in ("temperature", "top_p", "max_tokens", "stop", "n")}
            try:
                with lock:  # serialize ecash spends (wallet mutates on spend)
                    reply, _settle = wallet.chat(messages, model=model, stream=False, **kwargs)
            except RuntimeError as e:
                # insufficient ecash — tell the user how to top up
                return self._json(402, {"error": {
                    "message": f"{e}. Run `anon-router claim <credits>` to add ecash.",
                    "type": "insufficient_balance"}})
            except Exception as e:
                return self._json(502, {"error": str(e)})
            if want_stream:
                self._stream(reply)
            else:
                self._json(200, reply)

        def _messages(self, body: dict):
            """Anthropic Messages API (POST /v1/messages) for Claude Code etc.:
            translate to the OpenAI lane, pay ecash, translate the answer back."""
            import anthropic_proxy as ap
            model_out = body.get("model", "")
            oreq = ap.to_openai(body)
            oreq["model"] = ap.map_model(model_out, _available_models())  # valid live id

            if not body.get("stream"):
                oreq.pop("stream", None)
                msgs, model = oreq.pop("messages"), oreq.pop("model")
                try:
                    with lock:
                        reply, _ = wallet.chat(msgs, model=model, stream=False, **oreq)
                except RuntimeError as e:
                    return self._json(402, {"type": "error", "error": {
                        "type": "insufficient_balance",
                        "message": f"{e}. Run `anon-router claim <credits>`."}})
                except Exception as e:
                    return self._json(502, {"type": "error", "error": {"message": str(e)}})
                if isinstance(reply, dict) and reply.get("error"):  # upstream error body
                    e = reply["error"]
                    return self._json(400, {"type": "error", "error": {
                        "type": "api_error",
                        "message": e.get("message", str(e)) if isinstance(e, dict) else str(e)}})
                return self._json(200, ap.to_anthropic(reply, model_out))

            # streaming (Claude Code requires it)
            oreq["stream"] = True
            oreq["stream_options"] = {"include_usage": True}
            # Consume the ENTIRE upstream stream UNDER the wallet lock (prod-side
            # I/O is fast), peel the in-band change, and finish_stream — THEN
            # release the lock and write to the client. The lock therefore never
            # spans client I/O: holding it across the client write deadlocks a
            # client like Claude Code that fires concurrent requests (the 2nd
            # blocks on the lock while the 1st blocks writing to a not-yet-draining
            # client). It also serializes the single `pending` slot cleanly. Trade:
            # the Anthropic client isn't token-streamed (the full answer arrives at
            # once); headless `claude -p` is unaffected.
            buffered = []
            change_holder = {}
            try:
                with lock:
                    resp = wallet.open_stream(oreq)
                    try:
                        for line in resp.iter_lines():
                            s = line.strip() if isinstance(line, str) else line.decode(errors="ignore").strip()
                            if s == "event: x-cash-change":
                                change_holder["seen"] = True
                                continue
                            if change_holder.get("seen") and s.startswith("data: "):
                                try:
                                    change_holder["payload"] = json.loads(s[6:])
                                except Exception:
                                    pass
                                change_holder["seen"] = False
                                continue
                            buffered.append(line)
                    finally:
                        resp.close()
                        try:
                            wallet.finish_stream(change_holder.get("payload"))
                        except Exception:
                            pass
            except RuntimeError as e:
                return self._json(402, {"type": "error", "error": {
                    "type": "insufficient_balance",
                    "message": f"{e}. Run `anon-router claim <credits>`."}})
            except Exception as e:
                return self._json(502, {"type": "error", "error": {"message": str(e)}})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for sse in ap.stream_anthropic(iter(buffered), model_out):
                    self.wfile.write(sse.encode())
                    self.wfile.flush()
            except Exception:
                pass

        def _stream(self, reply: dict):
            """Emit the completion as OpenAI-compatible SSE (one content chunk +
            stop), so streaming clients work without token-by-token proxying."""
            content = reply["choices"][0]["message"].get("content", "")
            cid, model = reply.get("id", "chatcmpl"), reply.get("model", "")

            def chunk(delta, finish):
                return "data: " + json.dumps({
                    "id": cid, "object": "chat.completion.chunk", "model": model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }) + "\n\n"

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            for part in (chunk({"role": "assistant", "content": content}, None),
                         chunk({}, "stop"), "data: [DONE]\n\n"):
                self.wfile.write(part.encode())

    srv = ThreadingHTTPServer((host, port), Handler)
    bal = wallet.balance()
    acct_bal = 0
    if wallet.account:
        try:
            acct_bal = wallet.account_status().get("balance", 0)
        except Exception:
            pass
    base = f"http://{host}:{port}"
    print(f"\nanon-router ecash proxy  →  router {wallet.url}", flush=True)
    print(f"  OpenAI tools (Cursor, aider, SDK):  base_url = {base}/v1   (api_key = anything)",
          flush=True)
    print("  Claude Code — run it against this proxy with two env vars:", flush=True)
    print(f"      export ANTHROPIC_BASE_URL={base}", flush=True)
    print("      export ANTHROPIC_API_KEY=anon-router      # any non-empty value", flush=True)
    print("      claude              # every request now pays private ecash", flush=True)
    print(f"  spendable ecash: {bal}   (drained from the account at startup)", flush=True)
    if bal == 0:
        print("  empty wallet — fund once, then it's all claimed to ecash up front:", flush=True)
        print("      anon-router redeem <voucher>      # or: deposit 0.001 --key <key.json>",
              flush=True)
    print("  the provider sees only the router — never who paid. Ctrl-C to stop.\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
