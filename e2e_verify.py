"""End-to-end verification: drives the whole stack and asserts each stage.

Assumes anvil + router + watcher are up (run_e2e.sh starts them). Exercises:
  1. mint an anonymous API key
  2. deposit ETH on-chain -> watcher credits the key
  3. real inference call with the key (custodial simple lane)
  4. balance was debited by the metered cost
  5. confetti channel lane -> on-chain honest close pays the exact split
  6. on-chain fraud: stale close is challenged and forfeits the deposit

Exit 0 iff every stage passes. Prints a per-stage report.
"""
import os
import subprocess
import sys
import time

import httpx
from web3 import Web3

RPC = os.environ.get("RPC", "http://127.0.0.1:8545")
ROUTER = os.environ.get("ROUTER", "http://127.0.0.1:8402").rstrip("/")
VAULT = os.environ["VAULT"]
CONFETTI = os.environ["CONFETTI"]
MODEL = os.environ.get("E2E_MODEL", "openai/gpt-4o-mini")
# Anvil dev keys (public throwaways).
ALICE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
BOB_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
DEPLOYER = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def main():
    w3 = Web3(Web3.HTTPProvider(RPC))
    http = httpx.Client(timeout=60)

    print("STAGE 1-4: custodial simple lane (deposit -> key -> inference)")
    acct = http.post(f"{ROUTER}/account/new").json()
    key = acct["api_key"]
    check("mint API key", key.startswith("sk-anon-"), key[:20] + "...")

    # deposit 0.02 ETH referencing key_hash
    vault = w3.eth.contract(address=Web3.to_checksum_address(VAULT), abi=[{
        "inputs": [{"name": "keyHash", "type": "bytes32"}], "name": "deposit",
        "outputs": [], "stateMutability": "payable", "type": "function"}])
    alice = w3.eth.account.from_key(ALICE_KEY)
    kh = acct["key_hash"]
    tx = vault.functions.deposit(bytes.fromhex(kh[2:])).build_transaction({
        "from": alice.address, "value": w3.to_wei("0.02", "ether"),
        "nonce": w3.eth.get_transaction_count(alice.address),
        "gas": 100000, "gasPrice": w3.eth.gas_price})
    rcpt = w3.eth.wait_for_transaction_receipt(
        w3.eth.send_raw_transaction(alice.sign_transaction(tx).raw_transaction))
    check("on-chain deposit mined", rcpt.status == 1)

    expected = int(0.02 * acct["credits_per_eth"])
    bal = 0
    for _ in range(20):
        bal = http.get(f"{ROUTER}/account/status",
                       headers={"Authorization": f"Bearer {key}"}).json()["balance"]
        if bal >= expected:
            break
        time.sleep(1)
    check("watcher credited deposit", bal == expected, f"{bal} credits")

    r = http.post(f"{ROUTER}/v1/chat/completions",
                  headers={"Authorization": f"Bearer {key}"},
                  json={"model": MODEL, "messages": [
                      {"role": "user", "content": "Reply with exactly: e2e ok"}]})
    ok = r.status_code == 200 and "choices" in r.json()
    content = r.json().get("choices", [{}])[0].get("message", {}).get("content", "") if ok else r.text
    check("inference call with key", ok, content.strip()[:40])

    bal2 = http.get(f"{ROUTER}/account/status",
                    headers={"Authorization": f"Bearer {key}"}).json()["balance"]
    check("balance debited by metered cost", bal2 < bal, f"{bal} -> {bal2}")

    print("STAGE 5-6: trust-minimized channel lane (on-chain settlement)")
    rc = subprocess.run([sys.executable, "demo_m4b.py", CONFETTI],
                        capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
    honest = "Bob got exactly what he earned" in rc.stdout
    fraud = "fraud caught on-chain" in rc.stdout
    check("channel honest close pays exact split", honest)
    check("channel fraud is challenged + forfeited", fraud,
          "" if fraud else rc.stdout[-200:] + rc.stderr[-200:])

    passed = sum(1 for _, c, _ in results if c)
    print(f"\n{'=' * 50}\nE2E: {passed}/{len(results)} stages passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
