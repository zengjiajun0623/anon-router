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
        if b.get("model") == "openai/gpt-4o-mini" and "MALFORMED" in json.dumps(b):
            # A 200 with a non-JSON body, to prove the router refunds (not 500s)
            # after the spend already committed.
            self._send(b"this is not json <<<")
            return
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
        cb = base64.b64encode(
            json.dumps([{"B_": blind(os.urandom(16).hex())[0]} for _ in range(21)]).encode()).decode()
        r1 = httpx.post(f"{base}/v1/chat/completions",
                        headers={"X-Cash": sh, "X-Cash-Change": cb},
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

        # #1c — multimodal INPUT (image_url) must be rejected 400 BEFORE spend
        # (a short URL bypasses the length-based cost bound = operator over-spend).
        mmtok = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                 for t in w._select(2000)]
        mmh = base64.b64encode(json.dumps(mmtok).encode()).decode()
        mmcb = base64.b64encode(
            json.dumps([{"B_": blind(os.urandom(16).hex())[0]} for _ in range(21)]).encode()).decode()
        rmm = httpx.post(f"{base}/v1/chat/completions",
                         headers={"X-Cash": mmh, "X-Cash-Change": mmcb},
                         json={"model": "openai/gpt-4o-mini", "messages": [{"role": "user",
                               "content": [{"type": "image_url",
                                            "image_url": {"url": "http://x/y.png"}}]}]},
                         timeout=30)
        check("#1c multimodal image_url input rejected pre-spend (400)",
              rmm.status_code == 400)
        w.tokens.extend(mmtok)  # not spent -> return them for later checks

        # #4 — in-band change + idempotent recovery: drive a paid request at the
        # HTTP layer with blinded change blanks, read the change from the
        # X-Cash-Change response header, then RECOVER the same spend (X-Cash-
        # Recover) and require identical change signatures — no separate,
        # separately-timed redeem call exists anymore.
        spend = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                 for t in w._select(2000)]
        h = base64.b64encode(json.dumps(spend).encode()).decode()
        blanks = [blind(os.urandom(16).hex()) for _ in range(21)]  # (B_, r) pairs
        change_hdr = base64.b64encode(
            json.dumps([{"B_": b[0]} for b in blanks]).encode()).decode()
        hdrs = {"X-Cash": h, "X-Cash-Change": change_hdr}
        r = httpx.post(f"{base}/v1/chat/completions", headers=hdrs,
                       json={"model": "openai/gpt-4o-mini",
                             "messages": [{"role": "user", "content": "change please"}]},
                       timeout=30)
        inband = json.loads(base64.b64decode(r.headers["X-Cash-Change"]))
        change = inband["change"]
        check("#4 change delivered in-band on the spend response (no redeem call)",
              r.status_code == 200 and change > 0 and inband["signatures"])
        # Recover the identical spend: same tokens + same blanks + recover flag.
        rec = httpx.post(f"{base}/v1/chat/completions",
                         headers={**hdrs, "X-Cash-Recover": "1"},
                         json={"model": "recover",
                               "messages": [{"role": "user", "content": "."}]},
                         timeout=30)
        check("#4 recovery replays identical change signatures (idempotent)",
              rec.status_code == 200
              and rec.json()["signatures"] == inband["signatures"])
        # A never-spent token set under recovery returns 404 (client keeps it).
        fresh = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                 for t in w._select(1000)]
        fh = base64.b64encode(json.dumps(fresh).encode()).decode()
        rec404 = httpx.post(f"{base}/v1/chat/completions",
                            headers={"X-Cash": fh, "X-Cash-Change": change_hdr,
                                     "X-Cash-Recover": "1"},
                            json={"model": "recover",
                                  "messages": [{"role": "user", "content": "."}]},
                            timeout=30)
        check("#4 recovery of never-spent tokens is 404 (no spend)",
              rec404.status_code == 404)

        # #5 — NO double-ISSUE of change under concurrent recovery. Seed a
        # crash-recovered ('final', cost=0 => full refund) receipt for a spent
        # token set. (a) Two concurrent recovers with the SAME blanks (the real
        # idempotent-retry case) both return the SAME signatures — issued once.
        # (b) A recover with DIFFERENT blanks is rejected 409 (change is bound to
        # the first outputs), never a second independently-valid change set.
        import hashlib as _hl
        rtok = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                for t in w._select(4000)]
        rid = _hl.sha256(json.dumps(sorted(t["secret"] for t in rtok)).encode()).hexdigest()
        con = sqlite3.connect(dbp)
        for t in rtok:
            con.execute("INSERT OR IGNORE INTO spent(secret) VALUES (?)", (t["secret"],))
        con.execute("INSERT INTO receipts(id, prepaid, cost, state, ts) VALUES (?,?,0,'final',?)",
                    (rid, sum(t["amount"] for t in rtok), int(time.time())))
        con.commit(); con.close()
        rh = base64.b64encode(json.dumps(rtok).encode()).decode()
        cbA = base64.b64encode(json.dumps([{"B_": blind(os.urandom(16).hex())[0]} for _ in range(21)]).encode()).decode()
        cbB = base64.b64encode(json.dumps([{"B_": blind(os.urandom(16).hex())[0]} for _ in range(21)]).encode()).decode()

        def _recover(cb):
            return httpx.post(f"{base}/v1/chat/completions",
                              headers={"X-Cash": rh, "X-Cash-Change": cb, "X-Cash-Recover": "1"},
                              json={"model": "recover", "messages": [{"role": "user", "content": "."}]},
                              timeout=30)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa, fb = ex.submit(_recover, cbA), ex.submit(_recover, cbA)  # SAME blanks
            ra, rb = fa.result(), fb.result()
        check("#5a concurrent recover, same blanks: both 200, identical sigs (issued once)",
              ra.status_code == 200 and rb.status_code == 200
              and ra.json()["signatures"] == rb.json()["signatures"]
              and len(ra.json()["signatures"]) > 0)
        rdiff = _recover(cbB)  # different blanks -> bound to the first outputs
        check("#5b recover with DIFFERENT blanks rejected 409 (no second issuance)",
              rdiff.status_code == 409)

        # #6 — a malformed 200 upstream body AFTER the spend must FULL-REFUND
        # in-band (never a bare 500 that strands the burned tokens).
        mtok = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                for t in w._select(3000)]
        mh = base64.b64encode(json.dumps(mtok).encode()).decode()
        mprepaid = sum(t["amount"] for t in mtok)
        mcb = base64.b64encode(
            json.dumps([{"B_": blind(os.urandom(16).hex())[0]} for _ in range(21)]).encode()).decode()
        rm = httpx.post(f"{base}/v1/chat/completions",
                        headers={"X-Cash": mh, "X-Cash-Change": mcb},
                        json={"model": "openai/gpt-4o-mini",
                              "messages": [{"role": "user", "content": "MALFORMED please"}]},
                        timeout=30)
        minband = json.loads(base64.b64decode(rm.headers["X-Cash-Change"]))
        check("#6 malformed upstream 200 -> full refund in-band (not a 500)",
              rm.status_code == 502 and minband["change"] == mprepaid
              and minband["cost"] == 0 and minband["signatures"])

        # #3 — crash recovery: seed a 'pending' receipt (as a crash would leave),
        # restart the router, and confirm it is finalized to a full refund.
        proc.terminate(); proc.wait()
        con = sqlite3.connect(dbp)
        con.execute("INSERT INTO receipts(id, prepaid, cost, state, ts) "
                    "VALUES ('crashed', 1234, 0, 'pending', 0)")
        con.commit(); con.close()
        proc = boot()
        con = sqlite3.connect(dbp)
        row = con.execute("SELECT state, cost FROM receipts WHERE id='crashed'").fetchone()
        con.close()
        check("#3 crashed pending receipt recovered to full refund (cost=0, final)",
              row is not None and row[0] == "final" and row[1] == 0)

        print(f"\nMONEY-SAFETY E2E: {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
