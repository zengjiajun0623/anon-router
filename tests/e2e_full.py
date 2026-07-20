#!/usr/bin/env python3
"""End-to-end verification loop for anon-router.

Exercises the whole stack against a running deployment (default: the live
hosted service). Reusable for mainnet: point ROUTER_URL / RPC / VAULT at the
target. Does a REAL on-chain deposit to drive the watcher, then verifies the
private ecash lane, streaming, change, double-spend rejection, idempotency,
and the safety controls.

  ROUTER_URL=https://anon-router-production.up.railway.app \
  RPC=<sepolia rpc> VAULT=<vault addr> DEPLOYER_KEY_FILE=.sepolia-deployer.json \
  python tests/e2e_full.py
"""
import base64
import concurrent.futures as cf
import json
import os
import sys
import time

import httpx
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mint import blind, decompose, unblind  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path, default=""):
    try:
        return open(path).read().strip()
    except OSError:
        return default


ROUTER = os.environ.get("ROUTER_URL", "https://anon-router-production.up.railway.app").rstrip("/")
RPC = os.environ.get("RPC") or _read("/tmp/sepolia_rpc.txt", "https://ethereum-sepolia-rpc.publicnode.com")
VAULT = os.environ.get("VAULT") or _read("/tmp/sepolia_vault.txt")
KEYFILE = os.environ.get("DEPLOYER_KEY_FILE", os.path.join(ROOT, ".sepolia-deployer.json"))
DEPOSIT_ETH = float(os.environ.get("DEPOSIT_ETH", "0.001"))
MODEL = os.environ.get("E2E_MODEL", "openai/gpt-4o-mini")

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def keys_of(http):
    return http.get(f"{ROUTER}/mint/keys", timeout=30).json()


def unblind_sigs(pubkeys, secs, sigs):
    return [{"amount": d, "secret": s, "C": unblind(sig["C_"], r, pubkeys[str(d)])}
            for (d, s, r), sig in zip(secs, sigs)]


def claim(http, key, amount, idem=None):
    secs, outs = [], []
    for d in decompose(amount):
        s = os.urandom(32).hex()
        B, r = blind(s)
        secs.append((d, s, r))
        outs.append({"amount": d, "B_": B})
    h = {"Authorization": f"Bearer {key}"}
    if idem:
        h["Idempotency-Key"] = idem
    resp = http.post(f"{ROUTER}/mint/claim", json={"outputs": outs}, headers=h, timeout=45)
    if resp.status_code != 200:
        return resp.status_code, None
    return 200, unblind_sigs(keys_of(http)["pubkeys"], secs, resp.json()["signatures"])


def main():
    print(f"anon-router E2E loop → {ROUTER}\n")
    http = httpx.Client(timeout=60)

    # 1. health + safety controls
    hz = http.get(f"{ROUTER}/healthz").json()
    check("healthz ok + secret-free", hz.get("status") == "ok" and "sk-or" not in json.dumps(hz), str(hz))
    check("faucet disabled (403)", http.post(f"{ROUTER}/mint/topup", json={"outputs": [{"amount": 1, "B_": "02aa"}]}).status_code == 403)
    check("channel lane gated (503)", http.post(f"{ROUTER}/channel/open", json={}).status_code == 503)
    check("site + app.js served", http.get(f"{ROUTER}/").status_code == 200 and http.get(f"{ROUTER}/app.js").status_code == 200)

    # 2. account + REAL on-chain deposit -> watcher credits
    w3 = Web3(Web3.HTTPProvider(RPC))
    dep = w3.eth.account.from_key(json.load(open(KEYFILE))["private_key"])
    acct = http.post(f"{ROUTER}/account/new").json()
    key, kh = acct["api_key"], acct["key_hash"]
    check("mint anonymous key", key.startswith("sk-anon-"))
    vault = w3.eth.contract(address=Web3.to_checksum_address(VAULT), abi=[{"inputs": [{"name": "keyHash", "type": "bytes32"}], "name": "deposit", "outputs": [], "stateMutability": "payable", "type": "function"}])
    tx = vault.functions.deposit(bytes.fromhex(kh[2:])).build_transaction({"from": dep.address, "value": w3.to_wei(DEPOSIT_ETH, "ether"), "nonce": w3.eth.get_transaction_count(dep.address), "gas": 100000, "gasPrice": w3.eth.gas_price})
    txh = w3.eth.send_raw_transaction(dep.sign_transaction(tx).raw_transaction)
    print(f"  deposit tx {txh.hex()} ({DEPOSIT_ETH} ETH) — waiting for confirmation + watcher…")
    w3.eth.wait_for_transaction_receipt(txh, timeout=200)
    expected = int(DEPOSIT_ETH * acct["credits_per_eth"])
    bal = 0
    for _ in range(60):
        bal = http.get(f"{ROUTER}/account/status", headers={"Authorization": f"Bearer {key}"}).json()["balance"]
        if bal >= expected:
            break
        time.sleep(5)
    check("watcher credited real deposit", bal == expected, f"{bal}/{expected} credits")

    # 3. claim ecash + idempotency (same key twice -> one debit)
    idem = os.urandom(16).hex()
    code1, toks = claim(http, key, 5000, idem=idem)
    code2, _ = claim(http, key, 5000, idem=idem)  # replay
    bal_after = http.get(f"{ROUTER}/account/status", headers={"Authorization": f"Bearer {key}"}).json()["balance"]
    check("claim ecash", code1 == 200 and toks)
    check("idempotent claim: debited once", bal_after == expected - 5000, f"balance {bal_after}")

    # 4. non-streaming private inference
    spend = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]} for t in toks if t["amount"] >= 512][:1]
    hdr = {"X-Cash": base64.b64encode(json.dumps(spend).encode()).decode()}
    r = http.post(f"{ROUTER}/v1/chat/completions", headers=hdr, json={"model": MODEL, "messages": [{"role": "user", "content": "Reply with exactly: e2e ok"}]})
    out = r.json()["choices"][0]["message"]["content"].strip().lower() if r.status_code == 200 else str(r.status_code)
    check("private inference (non-stream)", "e2e ok" in out, out[:40])

    # 5. streaming inference
    spend2 = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]} for t in toks if t["amount"] >= 512][1:2] or [t for t in toks][:1]
    hdr2 = {"X-Cash": base64.b64encode(json.dumps([{"amount": t["amount"], "secret": t["secret"], "C": t["C"]} for t in spend2]).encode()).decode()}
    got_delta = False
    with http.stream("POST", f"{ROUTER}/v1/chat/completions", headers=hdr2, json={"model": MODEL, "stream": True, "messages": [{"role": "user", "content": "count: 1 2 3"}]}) as s:
        for line in s.iter_lines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                try:
                    if json.loads(line[6:])["choices"][0]["delta"].get("content"):
                        got_delta = True
                except Exception:
                    pass
    check("private inference (streaming)", got_delta)

    # 6. double-spend rejection (concurrent)
    _, toks2 = claim(http, key, 2000)
    ds = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]} for t in toks2 if t["amount"] >= 512][:1]
    dh = {"X-Cash": base64.b64encode(json.dumps(ds).encode()).decode()}

    def spend_once():
        try:
            return http.post(f"{ROUTER}/v1/chat/completions", headers=dh, json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}, timeout=60).status_code
        except Exception:
            return 599
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        codes = [f.result() for f in [ex.submit(spend_once) for _ in range(6)]]
    check("concurrent double-spend: exactly one accepted", codes.count(200) == 1, f"200s={codes.count(200)} codes={sorted(codes)}")

    passed = sum(1 for _, ok in results if ok)
    print(f"\n{'=' * 52}\nE2E: {passed}/{len(results)} passed  →  {ROUTER}")
    print(f"vault: {VAULT}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
