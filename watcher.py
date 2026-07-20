"""Deposit watcher: turns on-chain CreditVault deposits into account credits.

Polls `Deposited(keyHash, amount, from)` events and calls the router's internal
/account/credit endpoint (idempotent per txhash). Run alongside the router.

  RPC=http://127.0.0.1:8545 VAULT=0x... ROUTER=http://127.0.0.1:8402 \
  CREDIT_SECRET=... CREDITS_PER_ETH=10000000 python watcher.py
"""
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
    from_block = w3.eth.block_number
    print(f"watcher: vault={VAULT} from block {from_block}, {CREDITS_PER_ETH} credits/ETH")
    http = httpx.Client(timeout=15)
    while True:
        try:
            latest = w3.eth.block_number
            if latest >= from_block:
                logs = vault.events.Deposited().get_logs(
                    from_block=from_block, to_block=latest)
                for ev in logs:
                    kh = "0x" + ev["args"]["keyHash"].hex()
                    amount = ev["args"]["amount"]
                    credits = amount * CREDITS_PER_ETH // 10**18
                    txhash = ev["transactionHash"].hex()
                    resp = http.post(
                        f"{ROUTER}/account/credit",
                        headers={"X-Credit-Secret": CREDIT_SECRET},
                        json={"key_hash": kh, "credits": credits, "txhash": txhash},
                    )
                    print(f"  credited {credits} to {kh[:14]}.. ({resp.json().get('status')})")
                from_block = latest + 1
        except Exception as e:  # keep the watcher alive across transient RPC errors
            print(f"  watcher error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
