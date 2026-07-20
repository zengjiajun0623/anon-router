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


def run_proxy(wallet, host: str, port: int, daemon_key: str = "",
              default_model: str = "openai/gpt-4o-mini") -> None:
    lock = threading.Lock()

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
            model = body.get("model") or default_model
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
    print(f"ecash proxy on http://{host}:{port}/v1  ·  router {wallet.url}  ·  "
          f"balance {wallet.balance()} credits", flush=True)
    print("point any OpenAI-compatible tool here; each request pays private ecash.",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
