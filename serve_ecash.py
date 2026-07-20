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
    # Auto-refill so an agent never stalls: when spendable ecash drops below the
    # low-water mark, silently claim more from the deposited account balance.
    refill_low = int(os.environ.get("ANON_REFILL_LOW", "4000"))
    refill_amt = int(os.environ.get("ANON_REFILL_AMOUNT", "100000"))

    def _refill_if_low():
        try:
            if wallet.balance() >= refill_low or not wallet.account:
                return
            acct_bal = wallet.account_status().get("balance", 0)
            if acct_bal > 0:
                wallet.claim_from_account(wallet.account["api_key"],
                                          min(acct_bal, refill_amt))
        except Exception:
            pass  # best-effort; a truly empty wallet still returns a clear 402

    # First-run onboarding: make sure there's an account to fund.
    if not wallet.account and wallet.balance() == 0:
        try:
            wallet.new_account()
        except Exception:
            pass

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
            if not self.path.rstrip("/").endswith("/chat/completions"):
                return self._json(404, {"error": "not found"})
            if not self._authed():
                return self._json(401, {"error": "missing or invalid daemon key"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                return self._json(400, {"error": "invalid JSON body"})
            messages = body.get("messages")
            if not messages:
                return self._json(400, {"error": "messages required"})
            model = _map_model(body.get("model") or default_model)
            want_stream = bool(body.get("stream"))
            kwargs = {k: v for k, v in body.items()
                      if k in ("temperature", "top_p", "max_tokens", "stop", "n")}
            try:
                with lock:  # serialize ecash spends (wallet mutates on spend)
                    _refill_if_low()  # keep the agent from stalling
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
    print(f"\nanon-router ecash proxy  →  router {wallet.url}", flush=True)
    print(f"  point your agent/tool at:  base_url = http://{host}:{port}/v1   "
          f"(api_key = anything)", flush=True)
    print(f"  spendable ecash: {bal}   ·   account (auto-claimed as needed): {acct_bal}",
          flush=True)
    if bal + acct_bal < refill_low:
        print("  low/empty balance — fund it once, then it auto-tops-up:", flush=True)
        print("      anon-router deposit 0.001 --key <yourkey.json>", flush=True)
    print("  every request pays private, unlinkable ecash. Ctrl-C to stop.\n", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
