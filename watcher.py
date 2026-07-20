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
USDC_ADDRESS = os.environ.get("USDC_ADDRESS")
CREDITS_PER_USDC = (int(os.environ.get("CREDITS_PER_USDC", "10000"))
                    if USDC_ADDRESS else None)
POLL = float(os.environ.get("POLL_SECONDS", "2"))
CONFIRMATIONS = int(os.environ.get("CONFIRMATIONS", "3"))  # blocks to wait (reorg safety)
# NOTE: a reorg deeper than CONFIRMATIONS that orphans an already-credited
# deposit is not reconciled (the credit is not reversed). Acceptable for
# testnet; mainnet should raise CONFIRMATIONS and/or add orphan reconciliation.
# Persist the scan cursor so a restart resumes and never misses a deposit.
CURSOR = os.environ.get("WATCHER_CURSOR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".watcher_cursor"))


def _load_cursor(default):
    try:
        return int(open(CURSOR).read().strip())
    except (FileNotFoundError, ValueError):
        return default


def _save_cursor(block):
    # atomic: write a temp file then rename, so a crash mid-write can't leave a
    # truncated cursor that would reset the scan to chain head (losing deposits).
    tmp = CURSOR + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(block))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CURSOR)

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

TOKEN_DEPOSIT_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "keyHash", "type": "bytes32"},
        {"indexed": False, "name": "amount", "type": "uint256"},
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": False, "name": "token", "type": "address"},
    ],
    "name": "DepositedToken",
    "type": "event",
}


def main():
    w3 = Web3(Web3.HTTPProvider(RPC))
    abi = ABI + [TOKEN_DEPOSIT_ABI] if USDC_ADDRESS else ABI
    vault = w3.eth.contract(address=Web3.to_checksum_address(VAULT), abi=abi)
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
                if USDC_ADDRESS:
                    token_logs = vault.events.DepositedToken().get_logs(
                        from_block=from_block, to_block=safe,
                        argument_filters={
                            "token": Web3.to_checksum_address(USDC_ADDRESS)})
                    logs = sorted(
                        [*logs, *token_logs],
                        key=lambda ev: (ev["blockNumber"], ev["logIndex"]),
                    )
                # Only advance the cursor if EVERY credit in this range is
                # durably handled. A transient failure must NOT advance the
                # cursor (else the deposit is skipped forever). Credits are
                # idempotent (seen keyed on txhash+logIndex), so replay is safe.
                advanced = True
                for ev in logs:
                    kh = "0x" + ev["args"]["keyHash"].hex()
                    if USDC_ADDRESS and ev["event"] == "DepositedToken":
                        credits = ev["args"]["amount"] * CREDITS_PER_USDC // 10**6
                    else:
                        credits = ev["args"]["amount"] * CREDITS_PER_ETH // 10**18
                    txhash = ev["transactionHash"].hex()
                    try:
                        resp = http.post(
                            f"{ROUTER}/account/credit",
                            headers={"X-Credit-Secret": CREDIT_SECRET},
                            json={"key_hash": kh, "credits": credits,
                                  "txhash": txhash, "log_index": ev["logIndex"]},
                        )
                        resp.raise_for_status()
                        status = resp.json().get("status")
                    except Exception as e:
                        # transient (network/5xx): stop, retry this range next poll
                        print(f"  credit failed ({e}); will retry range from {from_block}")
                        advanced = False
                        break
                    print(f"  credited {credits} to {kh[:14]}.. ({status})")
                if advanced:
                    from_block = safe + 1
                    _save_cursor(from_block)
        except Exception as e:  # keep the watcher alive across transient RPC errors
            print(f"  watcher error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
