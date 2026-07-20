"""E2E proof of the six unlinkability fixes (Codex+Kimi review round).

  F1 no bearer-inference lane: the account key CANNOT pay for inference
  F3 in-band change: change rides the spend response; no /mint/change endpoints
  F4 fixed voucher face values (admin) + voucher redeemed via BODY, not URL
  F6 no status oracles (GET voucher/change gone); accounts store only key_hash

Self-contained: spawns a priced mock upstream + the real router.
  python tests/e2e_unlinkability.py
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
from mint import blind, decompose  # noqa: E402
from wallet import Wallet  # noqa: E402

PORT, UP = 8414, 9414


class Up(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self._send(json.dumps({
            "id": "x", "object": "chat.completion", "model": "openai/gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "echo"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "cost": 0.00002}}).encode())

    def do_GET(self):
        self._send(json.dumps({"data": [{"id": "openai/gpt-4o-mini",
            "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}}]}).encode())

    def _send(self, out):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _blanks(n=21):
    pairs = [blind(os.urandom(16).hex()) for _ in range(n)]
    hdr = base64.b64encode(json.dumps([{"B_": b[0]} for b in pairs]).encode()).decode()
    return hdr


def _is_offcurve(x):
    import ec
    try:
        ec.decompress(bytes([2]) + x.to_bytes(32, "big"))
        return False
    except ValueError:
        return True


def main() -> int:
    srv = HTTPServer(("127.0.0.1", UP), Up)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = os.path.join(ROOT, ".e2e_unlink_home")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    dbp = os.path.join(home, "state.db")
    env = {**os.environ, "OPENROUTER_API_KEY": "rk", "UPSTREAM": f"http://127.0.0.1:{UP}/v1",
           "DEV_FAUCET": "1", "CHANNEL_LANE_ENABLED": "0", "STATE_DB_PATH": dbp,
           "MINT_MASTER_PATH": os.path.join(home, "mint.hex"), "MAX_REQUEST_USD": "0.50"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--no-access-log"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{PORT}"
    for _ in range(40):
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.25)
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    try:
        w = Wallet(mint_url=base, path=os.path.join(home, "w.json"))
        w.topup(50000)

        # F1 — no bearer-inference lane. An account key must NOT buy inference.
        acct = w.new_account()
        r = httpx.post(f"{base}/v1/chat/completions",
                       headers={"Authorization": f"Bearer {acct['api_key']}"},
                       json={"model": "openai/gpt-4o-mini",
                             "messages": [{"role": "user", "content": "pay with account"}]},
                       timeout=30)
        check("F1 bearer account key cannot pay for inference (402, no lane)",
              r.status_code == 402)

        # F3 — in-band change; the old redeem endpoints are gone.
        spend = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                 for t in w._select(2000)]
        h = base64.b64encode(json.dumps(spend).encode()).decode()
        r = httpx.post(f"{base}/v1/chat/completions",
                       headers={"X-Cash": h, "X-Cash-Change": _blanks()},
                       json={"model": "openai/gpt-4o-mini",
                             "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
        check("F3 change delivered in the X-Cash-Change response header",
              r.status_code == 200 and "X-Cash-Change" in r.headers
              and "X-Change-Receipt" not in r.headers)
        rid = "0" * 64
        check("F6 GET /mint/change/<id> oracle removed (404)",
              httpx.get(f"{base}/mint/change/{rid}", timeout=10).status_code == 404)

        # F1/F3 spend requires change blanks (client always sends them).
        r = httpx.post(f"{base}/v1/chat/completions", headers={"X-Cash": h},
                       json={"model": "openai/gpt-4o-mini",
                             "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
        check("spend without change blanks is rejected (400)", r.status_code == 400)

        # F4/F6 — voucher redeemed by BODY (not URL); no status oracle.
        con = sqlite3.connect(dbp)
        con.execute("INSERT INTO vouchers(code, credits, state) VALUES ('ar-testcode', 50000, 'issued')")
        con.commit(); con.close()
        check("F6 GET /mint/voucher/<code> oracle removed (404/405)",
              httpx.get(f"{base}/mint/voucher/ar-testcode", timeout=10).status_code in (404, 405))
        outs = [{"amount": d, "B_": blind(os.urandom(16).hex())[0]} for d in decompose(50000)]
        rv = httpx.post(f"{base}/mint/redeem",
                        json={"code": "ar-testcode", "outputs": outs}, timeout=10)
        check("F4 voucher redeemed via request body (fixed face value)",
              rv.status_code == 200 and len(rv.json()["signatures"]) == len(outs))
        # Re-redeem with the SAME blinds -> idempotent replay of the cached sigs
        # (recovers a lost response without losing the voucher).
        rv2 = httpx.post(f"{base}/mint/redeem",
                         json={"code": "ar-testcode", "outputs": outs}, timeout=10)
        check("F4 idempotent replay: same blinds return the same signatures",
              rv2.status_code == 200 and rv2.json()["signatures"] == rv.json()["signatures"])
        # Re-redeem with DIFFERENT blinds -> uniform 400 (no double-issue, no
        # probing oracle): indistinguishable from an unknown code.
        outs_diff = [{"amount": d, "B_": blind(os.urandom(16).hex())[0]}
                     for d in decompose(50000)]
        rv3 = httpx.post(f"{base}/mint/redeem",
                         json={"code": "ar-testcode", "outputs": outs_diff}, timeout=10)
        rvx = httpx.post(f"{base}/mint/redeem",
                         json={"code": "does-not-exist", "outputs": outs_diff}, timeout=10)
        check("F4/F6 spent (diff blinds) + unknown codes both 400 (no oracle, no re-issue)",
              rv3.status_code == 400 and rvx.status_code == 400)

        # F1b — X-Cash-Recover must NEVER run inference, even on the free lane.
        w.topup(4000)
        ftok = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                for t in w._select(2000)]
        fhh = base64.b64encode(json.dumps(ftok).encode()).decode()
        rr = httpx.post(f"{base}/v1/chat/completions",
                        headers={"X-Cash": fhh, "X-Cash-Change": _blanks(),
                                 "X-Cash-Recover": "1"},
                        json={"model": "local/foo",
                              "messages": [{"role": "user", "content": "run free?"}]},
                        timeout=30)
        check("F1b recover header on the free lane runs no inference (404 not-spent)",
              rr.status_code == 404)

        # F3b — strict change-blank count: not exactly 21 is rejected BEFORE spend.
        spend2 = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                  for t in w._select(2000)]
        h2 = base64.b64encode(json.dumps(spend2).encode()).decode()
        few = base64.b64encode(json.dumps([{"B_": blind(os.urandom(16).hex())[0]}
                                           for _ in range(5)]).encode()).decode()
        rfew = httpx.post(f"{base}/v1/chat/completions",
                          headers={"X-Cash": h2, "X-Cash-Change": few},
                          json={"model": "openai/gpt-4o-mini",
                                "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
        check("F3b wrong change-blank count rejected pre-spend (400)",
              rfew.status_code == 400)
        # tokens were NOT spent — a proper spend with the same tokens still works.
        good = base64.b64encode(json.dumps([{"B_": blind(os.urandom(16).hex())[0]}
                                            for _ in range(21)]).encode()).decode()
        rok = httpx.post(f"{base}/v1/chat/completions",
                         headers={"X-Cash": h2, "X-Cash-Change": good},
                         json={"model": "openai/gpt-4o-mini",
                               "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
        check("F3b tokens survive the rejected attempt (re-spend succeeds)",
              rok.status_code == 200)

        # F3c — an OFF-CURVE change blank (valid format, not on the curve) must be
        # rejected BEFORE the spend, else _sign_change fails after burning tokens
        # -> pending receipt -> stale refund -> free inference.
        import ec  # noqa: E402
        offx = next(x for x in range(1, 2000)
                    if _is_offcurve(x))
        offpt = "02" + offx.to_bytes(32, "big").hex()
        good20 = [{"B_": blind(os.urandom(16).hex())[0]} for _ in range(20)]
        mixed = base64.b64encode(json.dumps(good20 + [{"B_": offpt}]).encode()).decode()
        spend3 = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                  for t in w._select(2000)]
        h3 = base64.b64encode(json.dumps(spend3).encode()).decode()
        roff = httpx.post(f"{base}/v1/chat/completions",
                          headers={"X-Cash": h3, "X-Cash-Change": mixed},
                          json={"model": "openai/gpt-4o-mini",
                                "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
        check("F3c off-curve change blank rejected pre-spend (400, no free inference)",
              roff.status_code == 400)
        goodall = base64.b64encode(json.dumps([{"B_": blind(os.urandom(16).hex())[0]}
                                               for _ in range(21)]).encode()).decode()
        rrez = httpx.post(f"{base}/v1/chat/completions",
                          headers={"X-Cash": h3, "X-Cash-Change": goodall},
                          json={"model": "openai/gpt-4o-mini",
                                "messages": [{"role": "user", "content": "hi"}]}, timeout=30)
        check("F3c tokens survive the off-curve rejection (re-spend succeeds)",
              rrez.status_code == 200)

        # F4b — a signing failure must NOT burn the voucher (sign before commit).
        con = sqlite3.connect(dbp)
        con.execute("INSERT INTO vouchers(code, credits, state) VALUES ('ar-v2', 50000, 'issued')")
        con.commit(); con.close()
        bad_outs = [{"amount": d, "B_": ("00" * 33)}  # malformed point -> sign fails
                    for d in decompose(50000)]
        rb = httpx.post(f"{base}/mint/redeem",
                        json={"code": "ar-v2", "outputs": bad_outs}, timeout=10)
        check("F4b malformed redeem rejected (400)", rb.status_code == 400)
        good_outs = [{"amount": d, "B_": blind(os.urandom(16).hex())[0]}
                     for d in decompose(50000)]
        rg = httpx.post(f"{base}/mint/redeem",
                        json={"code": "ar-v2", "outputs": good_outs}, timeout=10)
        check("F4b voucher NOT burned by the failed attempt (still redeemable)",
              rg.status_code == 200)

        # F6 — accounts store only the key_hash, never the raw bearer key.
        con = sqlite3.connect(dbp)
        cols = [c[1] for c in con.execute("PRAGMA table_info(accounts)").fetchall()]
        con.close()
        check("F6 accounts table stores key_hash only (no api_key column)",
              "key_hash" in cols and "api_key" not in cols)

        print(f"\nUNLINKABILITY E2E: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        proc.terminate(); proc.wait()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
