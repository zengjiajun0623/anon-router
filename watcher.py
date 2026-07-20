"""Deposit watcher: turns on-chain CreditVault deposits into account credits.

Polls `Deposited(keyHash, amount, from)` events and calls the router's internal
/account/credit endpoint (idempotent per txhash). Run alongside the router.

  RPC=http://127.0.0.1:8545 VAULT=0x... ROUTER=http://127.0.0.1:8402 \
  CREDIT_SECRET=... CREDITS_PER_ETH=10000000 python watcher.py
"""
import json
import os
import time

import httpx
from web3 import Web3

RPC = os.environ.get("RPC", "http://127.0.0.1:8545")
VAULT = os.environ["VAULT"]
ROUTER = os.environ.get("ROUTER", "http://127.0.0.1:8402").rstrip("/")
CREDIT_SECRET = os.environ["CREDIT_SECRET"]
CREDITS_PER_ETH = int(os.environ.get("CREDITS_PER_ETH", "10000000"))
POLL = float(os.environ.get("POLL_SECONDS", "2"))
CONFIRMATIONS = int(os.environ.get("CONFIRMATIONS", "2"))  # blocks to wait (reorg safety)
# Persist the scan cursor so a restart resumes and never misses a deposit.
CURSOR = os.environ.get("WATCHER_CURSOR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".watcher_cursor"))


def _load_cursor(default):
    try:
        return int(open(CURSOR).read().strip())
    except (FileNotFoundError, ValueError):
        return default


def _save_cursor(block):
    with open(CURSOR, "w") as f:
        f.write(str(block))

ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "keyHash", "type": "bytes32"},
        {"indexed": False, "name": "amount", "type": "uint256"},
        {"indexed": True, "name": "from", "type": "address"},
    ],
    "name": "Deposited",
    "type": "event",
}]


def main():
    w3 = Web3(Web3.HTTPProvider(RPC))
    vault = w3.eth.contract(address=Web3.to_checksum_address(VAULT), abi=ABI)
    from_block = _load_cursor(w3.eth.block_number)
    print(f"watcher: vault={VAULT} from block {from_block}, "
          f"{CREDITS_PER_ETH} credits/ETH, {CONFIRMATIONS} confirmations")
    http = httpx.Client(timeout=15)
    while True:
        try:
            safe = w3.eth.block_number - CONFIRMATIONS  # only scan confirmed blocks
            if safe >= from_block:
                logs = vault.events.Deposited().get_logs(
                    from_block=from_block, to_block=safe)
                for ev in logs:
                    kh = "0x" + ev["args"]["keyHash"].hex()
                    credits = ev["args"]["amount"] * CREDITS_PER_ETH // 10**18
                    txhash = ev["transactionHash"].hex()
                    resp = http.post(
                        f"{ROUTER}/account/credit",
                        headers={"X-Credit-Secret": CREDIT_SECRET},
                        json={"key_hash": kh, "credits": credits, "txhash": txhash},
                    )
                    print(f"  credited {credits} to {kh[:14]}.. ({resp.json().get('status')})")
                from_block = safe + 1
                _save_cursor(from_block)
        except Exception as e:  # keep the watcher alive across transient RPC errors
            print(f"  watcher error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
