"""E2E privacy proof: the upstream inference provider can never learn who paid
or which session — it only ever sees "the router".

A client deliberately injects identity/tracking fields (OpenAI `user`, OpenRouter
`metadata`, retention `store`) into paid (ecash) and free requests. We stand up a
capturing upstream, run the real router in front of it, and assert the router
forwards NONE of those fields and presents only its own key upstream.

    python tests/e2e_privacy.py     # self-contained; spawns the router
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from wallet import Wallet  # noqa: E402

CAPTURED: list = []
ROUTER_KEY = "router-openrouter-key-SECRET-" + os.urandom(4).hex()
PORT = 8412
UP_PORT = 9411


class Cap(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        CAPTURED.append({"body": body, "auth": self.headers.get("Authorization", "")})
        last = next((m["content"] for m in reversed(body.get("messages", []))
                     if m.get("role") == "user"), "")
        out = json.dumps({
            "id": "c", "object": "chat.completion", "model": body.get("model", "m"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": f"echo: {last}"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8,
                      "cost": 0.00001}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        # Price models like OpenRouter does, so the router's cost-bound uses real
        # (cheap) pricing rather than the conservative ceiling.
        out = json.dumps({"data": [
            {"id": "openai/gpt-4o-mini",
             "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
        ]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main() -> int:
    srv = HTTPServer(("127.0.0.1", UP_PORT), Cap)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    home = os.path.join(ROOT, ".e2e_privacy_home")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    env = {**os.environ, "OPENROUTER_API_KEY": ROUTER_KEY,
           "UPSTREAM": f"http://127.0.0.1:{UP_PORT}/v1",
           "LOCAL_UPSTREAM": f"http://127.0.0.1:{UP_PORT}/v1",
           "DEV_FAUCET": "1", "CHANNEL_LANE_ENABLED": "0"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--no-access-log"],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(40):
            try:
                if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.25)

        wallet_path = os.path.join(home, "wallet.json")
        w = Wallet(mint_url=base, path=wallet_path)
        w.topup(50000)

        # Every known identity/session/tracking/retention vector the client can try.
        TRACKING = ("user", "metadata", "store", "session_id", "trace")
        inject = dict(user="tracker@evil.com", metadata={"session": "sess-ABC"},
                      store=True, session_id="sess-ABC", trace={"id": "t1"})
        r1, _ = w.chat([{"role": "user", "content": "hello via ecash"}],
                       model="openai/gpt-4o-mini", **inject)
        r2, _ = w.chat([{"role": "user", "content": "hello via free"}],
                       model="local/qwen3:8b", **inject)

        ok = True

        def check(name, cond):
            nonlocal ok
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
            ok = ok and cond

        # what the router is ALLOWED to forward (mirror of server.UPSTREAM_ALLOWED_FIELDS)
        import server
        allowed = server.UPSTREAM_ALLOWED_FIELDS

        check("ecash + free requests reached upstream", len(CAPTURED) == 2)
        leaked = sorted({f for c in CAPTURED for f in TRACKING if f in c["body"]})
        check(f"no identity/tracking fields forwarded (leaked: {leaked or 'none'})", not leaked)
        stray = sorted({k for c in CAPTURED for k in c["body"] if k not in allowed})
        check(f"only allowlisted fields reach upstream (stray: {stray or 'none'})", not stray)
        check("every upstream request carried messages+model",
              all("messages" in c["body"] and "model" in c["body"] for c in CAPTURED))
        check("upstream never saw a client/payer identity in auth",
              all(c["auth"] in ("", f"Bearer {ROUTER_KEY}") for c in CAPTURED))
        paid = [c for c in CAPTURED if "ecash" in json.dumps(c["body"])]
        check("paid request carried the router's own key upstream",
              bool(paid) and paid[0]["auth"] == f"Bearer {ROUTER_KEY}")
        check("client still got real replies",
              r1["choices"][0]["message"]["content"].startswith("echo:")
              and r2["choices"][0]["message"]["content"].startswith("echo:"))

        print(f"\nPRIVACY E2E: {'PASS — provider sees only the router' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
