"""E2E money-safety proof for the four Codex-flagged bugs.

  #1 prepay must cover the request's bounded worst-case cost (no operator loss)
  #3 a receipt left 'pending' by a crash is recovered to a full refund on restart
  #4 change redemption is idempotent (a retried redeem returns the same signatures)

Self-contained: spawns a priced mock upstream + the real router. `python tests/e2e_money_safety.py`
"""
import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from mint import blind, decompose, unblind  # noqa: E402
from wallet import Wallet  # noqa: E402

PORT, UP = 8413, 9412
PRICING = {
    "openai/gpt-4o-mini": {"prompt": "0.00000015", "completion": "0.0000006"},  # cheap
    "pricey/big": {"prompt": "0.00001", "completion": "0.00004"},  # ~$40/1M out
}


FORWARDED: list = []  # bodies the router actually forwarded upstream


class Up(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        b = json.loads(self.rfile.read(n) or b"{}")
        FORWARDED.append(b)
        last = next((m["content"] for m in reversed(b.get("messages", []))
                     if m.get("role") == "user"), "")
        self._send(json.dumps({
            "id": "x", "object": "chat.completion", "model": b.get("model"),
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": f"echo: {last}"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10,
                      "cost": 0.00002}}).encode())

    def do_GET(self):
        self._send(json.dumps({"data": [{"id": k, "pricing": v}
                                        for k, v in PRICING.items()]}).encode())

    def _send(self, out):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main() -> int:
    srv = HTTPServer(("127.0.0.1", UP), Up)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = os.path.join(ROOT, ".e2e_money_home")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    dbp = os.path.join(home, "state.db")
    env = {**os.environ, "OPENROUTER_API_KEY": "rk", "UPSTREAM": f"http://127.0.0.1:{UP}/v1",
           "DEV_FAUCET": "1", "CHANNEL_LANE_ENABLED": "0", "STATE_DB_PATH": dbp,
           "MINT_MASTER_PATH": os.path.join(home, "mint.hex"), "MAX_REQUEST_USD": "0.50"}

    def boot():
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
             "--port", str(PORT), "--no-access-log"], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                if httpx.get(f"http://127.0.0.1:{PORT}/healthz", timeout=2).status_code == 200:
                    return p
            except Exception:
                pass
            time.sleep(0.25)
        return p

    proc = boot()
    base = f"http://127.0.0.1:{PORT}"
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    try:
        w = Wallet(mint_url=base, path=os.path.join(home, "w.json"))
        w.topup(50000)

        # #1 — prepay must cover the request's bounded worst-case. Fund a SMALL
        # wallet (~700 credits, above MIN_PREPAY) and pay a pricey model whose
        # bounded max cost is far higher: the router rejects (402) rather than
        # eating the difference.
        ws = Wallet(mint_url=base, path=os.path.join(home, "ws.json"))
        ws.topup(700)
        small = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                 for t in ws._select(700)]
        sh = base64.b64encode(json.dumps(small).encode()).decode()
        r1 = httpx.post(f"{base}/v1/chat/completions", headers={"X-Cash": sh},
                        json={"model": "pricey/big", "messages": [{"role": "user", "content": "x"}]},
                        timeout=30)
        check("#1 under-prepaid pricey request rejected (402)", r1.status_code == 402)

        # A cheap request with ample prepay still works — and cost-bypass fields
        # (huge max_tokens / max_completion_tokens / a pricey `models` fallback)
        # must be clamped or dropped before forwarding.
        reply, settle = w.chat([{"role": "user", "content": "hi cheap"}],
                               model="openai/gpt-4o-mini", prepay=4000,
                               max_tokens=999999, max_completion_tokens=999999,
                               models=["pricey/big"])
        check("#1 cheap request within prepay works",
              reply["choices"][0]["message"]["content"].startswith("echo:"))
        fwd = FORWARDED[-1]
        check("output clamped to MAX_OUTPUT_TOKENS upstream", fwd.get("max_tokens") <= 8192)
        check("max_completion_tokens canonicalized away", "max_completion_tokens" not in fwd)
        check("pricey `models` fallback stripped upstream", "models" not in fwd)

        # #4 — change redemption idempotency: drive a paid request at the HTTP
        # layer, then redeem the SAME change twice and require identical sigs.
        spend = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                 for t in w._select(2000)]
        h = base64.b64encode(json.dumps(spend).encode()).decode()
        r = httpx.post(f"{base}/v1/chat/completions", headers={"X-Cash": h},
                       json={"model": "openai/gpt-4o-mini",
                             "messages": [{"role": "user", "content": "change please"}]},
                       timeout=30)
        rcpt = r.headers.get("X-Change-Receipt")
        info = httpx.get(f"{base}/mint/change/{rcpt}", timeout=10).json()
        change = info["change"]
        outs = []
        for denom in decompose(change):
            sec = os.urandom(16).hex()
            bhex, _r = blind(sec)
            outs.append({"amount": denom, "B_": bhex})
        body = json.dumps({"outputs": outs}).encode()
        s1 = httpx.post(f"{base}/mint/change/{rcpt}", content=body, timeout=10)
        s2 = httpx.post(f"{base}/mint/change/{rcpt}", content=body, timeout=10)  # retry
        check("#4 change redeem is idempotent (both 200)",
              s1.status_code == 200 and s2.status_code == 200)
        check("#4 retried change returns identical signatures",
              s1.json()["signatures"] == s2.json()["signatures"] and change > 0)

        # #3 — crash recovery: seed a 'pending' receipt (as a crash would leave),
        # restart the router, and confirm it is finalized to a full refund.
        proc.terminate(); proc.wait()
        con = sqlite3.connect(dbp)
        con.execute("INSERT INTO receipts(id, prepaid, cost, state) VALUES ('crashed', 1234, 0, 'pending')")
        con.commit(); con.close()
        proc = boot()
        info = httpx.get(f"{base}/mint/change/crashed", timeout=10).json()
        check("#3 crashed pending receipt recovered to full refund",
              info["state"] == "final" and info["change"] == 1234)

        print(f"\nMONEY-SAFETY E2E: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
