"""Deposit watcher: turns on-chain CreditVault deposits into account credits.

Polls `Deposited(keyHash, amount, from)` events and calls the router's internal
/account/credit endpoint (idempotent per txhash:logIndex). Run alongside the
router.

  RPC=http://127.0.0.1:8545 VAULT=0x... ROUTER=http://127.0.0.1:8402 \
  CREDIT_SECRET=... CREDITS_PER_ETH=10000000 python watcher.py

Reorg safety (real-money mainnet)
---------------------------------
Three layers protect against chain reorganizations:

1. CONFIRMATIONS: only blocks buried at least CONFIRMATIONS deep are scanned,
   so a reorg shallower than CONFIRMATIONS happens entirely ABOVE the scan
   frontier and can never trigger a credit. Default is chain-derived: 12 on
   Ethereum mainnet (chain id 1), 3 on testnets/local dev. Override with
   CONFIRMATIONS=N. Mainnet operators should keep this at 12+.

2. Server-side idempotency: /account/credit dedups on `txhash:logIndex`
   (seen_deposits table), so even if a reorg re-orders/replays blocks inside the
   already-scanned range, the same deposit event is never credited twice.

3. Deep-reorg reconciliation: every credited deposit is tracked locally by
   (event_id, blockNumber, blockHash) until it is buried FINALITY_DEPTH deep.
   Each poll re-checks that those blocks are still canonical. If a reorg deeper
   than CONFIRMATIONS orphans an already-credited deposit, the watcher writes a
   halt flag, stops crediting AND stops scanning, and requires manual operator
   reconciliation before it will resume (clear the halt file). Halting the scan
   too - not just skipping the orphan - is deliberate: a re-included deposit can
   land at a different logIndex (a new event_id the server hasn't seen), so
   continuing to scan could double-credit it. FINALITY_DEPTH default is
   chain-derived: 64 on mainnet, 12 on testnets. Override with FINALITY_DEPTH=N.
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
USDC_ADDRESS = os.environ.get("USDC_ADDRESS")
CREDITS_PER_USDC = (int(os.environ.get("CREDITS_PER_USDC", "10000"))
                    if USDC_ADDRESS else None)
POLL = float(os.environ.get("POLL_SECONDS", "2"))
# CONFIRMATIONS / FINALITY_DEPTH default off the chain id (resolved in main once
# the RPC is up); an explicit env var always wins. See _chain_defaults().
_CONFIRMATIONS_ENV = os.environ.get("CONFIRMATIONS")
_FINALITY_DEPTH_ENV = os.environ.get("FINALITY_DEPTH")

# Persist the scan cursor so a restart resumes and never misses a deposit.
CURSOR = os.environ.get("WATCHER_CURSOR", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".watcher_cursor"))
# Local ledger of credited-but-not-yet-final deposits, used for deep-reorg
# reconciliation. JSONL of {event_id, key_hash, credits, block_number,
# block_hash, ts}; pruned once a deposit is FINALITY_DEPTH deep.
CREDITED_LEDGER = os.environ.get("WATCHER_CREDITED", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".watcher_credited"))
# Presence of this file means an orphaned credit was detected: the watcher will
# not credit or scan until an operator reconciles and removes the file.
HALT_FILE = os.environ.get("WATCHER_HALT", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".watcher_halt"))


def _chain_defaults(chain_id):
    """(confirmations, finality_depth) defaults for a chain id.

    Ethereum mainnet moves real money: wait 12 blocks before crediting and watch
    orphans for ~2 epochs (64 blocks). Testnets/local dev stay snappy at 3/12.
    """
    if chain_id == 1:
        return 12, 64
    return 3, 12


def _hexstr(v):
    """Normalize a HexBytes or hex string to lowercase, no 0x prefix, so hash
    comparisons work regardless of the web3/HexBytes prefix convention."""
    s = v if isinstance(v, str) else v.hex()
    return s[2:].lower() if s.startswith(("0x", "0X")) else s.lower()


def _atomic_write(path, text):
    # write a temp file then rename, so a crash mid-write can't leave a
    # truncated file behind.
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_cursor(default):
    try:
        return int(open(CURSOR).read().strip())
    except (FileNotFoundError, ValueError):
        return default


def _save_cursor(block):
    # atomic (see _atomic_write): a truncated cursor would reset the scan to
    # chain head and lose deposits.
    _atomic_write(CURSOR, str(block))


def _load_credited():
    """Load the credited-deposit ledger as {event_id: entry}."""
    out = {}
    try:
        with open(CREDITED_LEDGER) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    out[e["event_id"]] = e
                except (ValueError, KeyError):
                    continue  # tolerate a partial last line
    except FileNotFoundError:
        pass
    return out


def _save_credited(credited):
    text = "".join(
        json.dumps(e, separators=(",", ":")) + "\n" for e in credited.values())
    _atomic_write(CREDITED_LEDGER, text)


def _is_halted():
    return os.path.exists(HALT_FILE)


def _read_halt_reason():
    try:
        return open(HALT_FILE).read().strip()
    except OSError:
        return "(halt file present)"


def _set_halt(reason):
    # latch on first detection; keep the original reason/timestamp.
    if os.path.exists(HALT_FILE):
        return
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write(HALT_FILE, f"{stamp} {reason}\n")


def _reconcile(w3, credited, head, finality_depth):
    """Re-check that every tracked (not-yet-final) credited deposit still sits in
    a canonical block, and prune deposits that are now buried FINALITY_DEPTH deep.

    Returns (orphans, verified, pruned):
      orphans  - entries whose credited block is no longer canonical (a reorg
                 deeper than CONFIRMATIONS orphaned an already-credited deposit).
      verified - False if an RPC error made the check inconclusive: the caller
                 should skip crediting this poll but must NOT latch the halt flag.
      pruned   - True if finalized entries were dropped from `credited` in place.
    """
    final_below = head - finality_depth
    orphans = []
    pruned = False
    canon = {}  # block_number -> canonical hash (dedup fetches within a poll)
    for eid in list(credited):
        e = credited[eid]
        bn = e["block_number"]
        if bn <= final_below:
            # buried beyond the reorg window: treat as final, stop tracking.
            del credited[eid]
            pruned = True
            continue
        if bn not in canon:
            try:
                canon[bn] = _hexstr(w3.eth.get_block(bn)["hash"])
            except Exception as ex:
                # RPC hiccup: inconclusive, retry next poll. Do NOT treat a
                # failed fetch as an orphan (that would falsely halt).
                print(f"  reconcile: could not fetch block {bn} ({ex}); "
                      "skipping this poll")
                return [], False, pruned
        if canon[bn] != e["block_hash"]:
            orphans.append(e)
    return orphans, True, pruned


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
    chain_id = w3.eth.chain_id
    dconf, dfinal = _chain_defaults(chain_id)
    confirmations = int(_CONFIRMATIONS_ENV) if _CONFIRMATIONS_ENV else dconf
    finality_depth = int(_FINALITY_DEPTH_ENV) if _FINALITY_DEPTH_ENV else dfinal
    if finality_depth <= confirmations:
        # the reorg-watch window is (confirmations, finality_depth]; it must be
        # non-empty or a just-credited deposit would be pruned before any check.
        finality_depth = confirmations + 1
    abi = ABI + [TOKEN_DEPOSIT_ABI] if USDC_ADDRESS else ABI
    vault = w3.eth.contract(address=Web3.to_checksum_address(VAULT), abi=abi)
    from_block = _load_cursor(w3.eth.block_number)
    credited = _load_credited()
    print(f"watcher: vault={VAULT} chain={chain_id} from block {from_block}, "
          f"{CREDITS_PER_ETH} credits/ETH, {confirmations} confirmations, "
          f"finality_depth {finality_depth}, tracking {len(credited)} credit(s)")
    if chain_id == 1 and confirmations < 12:
        print("  WARNING: mainnet (chain 1) with <12 confirmations is unsafe "
              "for real money; set CONFIRMATIONS=12 or higher.")
    if _is_halted():
        print(f"  HALTED at startup: {_read_halt_reason()}")
        print(f"  Reconcile the orphaned credit(s), then remove {HALT_FILE} "
              "to resume.")
    http = httpx.Client(timeout=15)
    halted_logged = False
    while True:
        try:
            if _is_halted():
                if not halted_logged:
                    print(f"  HALTED: {_read_halt_reason()}")
                    print("  Not crediting or scanning until reconciled; "
                          f"remove {HALT_FILE} to resume.")
                    halted_logged = True
                time.sleep(POLL)
                continue
            halted_logged = False

            head = w3.eth.block_number
            # 1. reconcile already-credited deposits against the canonical chain.
            orphans, verified, pruned = _reconcile(
                w3, credited, head, finality_depth)
            if pruned:
                _save_credited(credited)
            if orphans:
                details = "; ".join(
                    f"{e['event_id']} credits={e['credits']} "
                    f"block={e['block_number']} expected_hash=0x{e['block_hash']}"
                    for e in orphans)
                reason = ("deep reorg orphaned already-credited deposit(s): "
                          + details)
                _set_halt(reason)
                print(f"  CRITICAL: {reason}")
                print("  Halting: manual reconciliation required.")
                time.sleep(POLL)
                continue  # next iteration takes the halted branch
            if not verified:
                # inconclusive reconciliation (RPC error): don't credit this poll.
                time.sleep(POLL)
                continue

            # 2. scan confirmed blocks and credit new deposits.
            safe = head - confirmations  # only scan buried, reorg-safe blocks
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
                # idempotent (seen keyed on txhash:logIndex), so replay is safe:
                # a reorg shallower than CONFIRMATIONS also can't double-credit,
                # because it happens above `safe` and is never scanned twice.
                advanced = True
                new_entries = []
                for ev in logs:
                    kh = "0x" + ev["args"]["keyHash"].hex()
                    if USDC_ADDRESS and ev["event"] == "DepositedToken":
                        credits = ev["args"]["amount"] * CREDITS_PER_USDC // 10**6
                    else:
                        credits = ev["args"]["amount"] * CREDITS_PER_ETH // 10**18
                    txhash = ev["transactionHash"].hex()
                    log_index = ev["logIndex"]
                    try:
                        resp = http.post(
                            f"{ROUTER}/account/credit",
                            headers={"X-Credit-Secret": CREDIT_SECRET},
                            json={"key_hash": kh, "credits": credits,
                                  "txhash": txhash, "log_index": log_index},
                        )
                        resp.raise_for_status()
                        status = resp.json().get("status")
                    except Exception as e:
                        # transient (network/5xx): stop, retry this range next poll
                        print(f"  credit failed ({e}); will retry range from {from_block}")
                        advanced = False
                        break
                    if status in ("credited", "already_credited"):
                        # track for deep-reorg reconciliation
                        new_entries.append({
                            "event_id": f"{txhash}:{log_index}",
                            "key_hash": kh,
                            "credits": credits,
                            "block_number": ev["blockNumber"],
                            "block_hash": _hexstr(ev["blockHash"]),
                            "ts": int(time.time()),
                        })
                    print(f"  credited {credits} to {kh[:14]}.. ({status})")
                if advanced:
                    changed = False
                    for e in new_entries:
                        if e["event_id"] not in credited:
                            credited[e["event_id"]] = e
                            changed = True
                    # persist the ledger BEFORE the cursor: if we crash between,
                    # the range is re-scanned and re-credited idempotently.
                    if changed:
                        _save_credited(credited)
                    from_block = safe + 1
                    _save_cursor(from_block)
        except Exception as e:  # keep the watcher alive across transient RPC errors
            print(f"  watcher error: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()
