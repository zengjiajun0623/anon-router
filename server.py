"""anon-router: payer-anonymous OpenAI-compatible inference proxy.

Payment: prepaid blind-signature tokens (see mint.py). The server cannot link
a request to the deposit that funded it. Overpayment comes back as blind
change via a one-time receipt.

Run: uvicorn server:app --host 127.0.0.1 --port 8402
"""
import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import ec
from mint import DENOMS, Mint, decompose
from confetti.chain import ChannelRecord
from confetti.channel import Contract, Recipient
from confetti.relation import ClearWitnessProver
from confetti.sp1 import RealSP1Prover
from confetti.wire import payment_from_j, sig_to_j

ROOT = os.path.dirname(os.path.abspath(__file__))


def _load_env() -> None:
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())


_load_env()

OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")
if not OPENROUTER_KEY:
    raise RuntimeError("OPENROUTER_API_KEY must be set")
UPSTREAM = os.environ.get("UPSTREAM", "https://openrouter.ai/api/v1")
CREDIT_USD = float(os.environ.get("CREDIT_USD", "0.0001"))  # 1 credit = $0.0001
MARKUP = float(os.environ.get("MARKUP", "1.0"))
MIN_PREPAY = int(os.environ.get("MIN_PREPAY", "500"))  # credits required up front
DEV_FAUCET = os.environ.get("DEV_FAUCET", "0") == "1"
FAUCET_MAX = int(os.environ.get("FAUCET_MAX", "500000"))  # per topup call, dev only
CHANNEL_LANE_ENABLED = os.environ.get("CHANNEL_LANE_ENABLED", "0") == "1"
DAILY_USD_CAP = float(os.environ.get("DAILY_USD_CAP", "0"))
MAX_REQUEST_USD = max(CREDIT_USD, float(os.environ.get("MAX_REQUEST_USD", "0.50")))
ACCOUNT_RATE_PER_MIN = int(os.environ.get("ACCOUNT_RATE_PER_MIN", "120"))
# Cost-bounding: cap output tokens per request and price the worst case so the
# prepay/balance always covers the maximum the operator can be billed upstream.
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "8192"))
# TRUE worst-case $/token ceiling for models with no live pricing — must be an
# UPPER bound on any real model so an unknown/typo model can never be
# under-reserved (~$600 / 1M tokens is above today's priciest). Such requests
# are then usually rejected by MAX_REQUEST_USD, which is the safe direction.
PRICE_CEIL_PER_TOKEN = float(os.environ.get("PRICE_CEIL_PER_TOKEN", "0.0006"))
# Hard reject on oversized input (bounds cost estimation error + a DoS lever).
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "2000000"))
# A 'pending' receipt older than this was left by a crash (real requests finish
# within the ~300s upstream timeout); only such receipts are auto-refunded, so
# recovery never touches another worker's in-flight request.
# Bound streaming upstream calls: a stream idle this long (no new tokens) is cut,
# so a hung/malicious upstream can't hold a router connection open forever.
STREAM_READ_TIMEOUT_S = float(os.environ.get("STREAM_READ_TIMEOUT_S", "120"))
# Hard ceiling on total streaming wall-clock (checked between chunks).
MAX_STREAM_TOTAL_SEC = max(60, int(os.environ.get("MAX_STREAM_TOTAL_SEC", "600")))
# A pending receipt older than this is treated as crash-abandoned and refunded.
# It is DERIVED to always exceed the longest a live stream can run — the total
# ceiling plus one more idle read (the deadline is checked between chunks) plus a
# margin — so the sweep can NEVER race a live stream regardless of env overrides.
RECEIPT_STALE_SEC = max(
    int(os.environ.get("RECEIPT_STALE_SEC", "900")),
    MAX_STREAM_TOTAL_SEC + int(STREAM_READ_TIMEOUT_S) + 120)

if DEV_FAUCET and urlparse(UPSTREAM).hostname not in {"localhost", "127.0.0.1"}:
    message = "REFUSING TO START: DEV_FAUCET requires a localhost upstream"
    print(message)
    raise RuntimeError(message)

# Model-prefix routing. "local/<model>" strips the prefix and goes to the free
# lane (3080 Ollama via ssh tunnel); anything else goes to the paid default.
UPSTREAMS = {
    "local": {
        "base": os.environ.get("LOCAL_UPSTREAM", "http://127.0.0.1:11435/v1"),
        "key": None,
        "free": True,
    },
}


def resolve_route(model: str) -> tuple[str, str | None, bool, str]:
    """-> (base_url, api_key, free, upstream_model_name)"""
    prefix, _, rest = model.partition("/")
    if rest and prefix in UPSTREAMS:
        u = UPSTREAMS[prefix]
        return u["base"], u["key"], u["free"], rest
    return UPSTREAM, OPENROUTER_KEY, False, model


# Strict allowlist of fields the router forwards to the upstream provider: only
# inference parameters, never identity/session/tracking/retention. Anything not
# listed here (user, metadata, store, session_id, trace, and any future field)
# is dropped so it cannot reach the provider — see chat().
UPSTREAM_ALLOWED_FIELDS = frozenset({
    "model", "messages", "prompt", "stream", "stream_options",
    "temperature", "top_p", "top_k", "min_p", "top_a",
    "max_tokens", "max_completion_tokens", "n", "stop", "seed",
    "presence_penalty", "frequency_penalty", "repetition_penalty",
    "logit_bias", "logprobs", "top_logprobs",
    "response_format", "tools", "tool_choice", "parallel_tool_calls",
    "functions", "function_call", "prediction",
    "reasoning", "reasoning_effort", "verbosity", "usage",
    # NOTE: "modalities"/"audio" are deliberately NOT forwarded. This is a TEXT
    # MVP: audio output isn't detected by the streaming produced-scan (=> would be
    # free) and isn't covered by _bound_cost's token pricing. Dropping them makes
    # every request text, so produced-detection and the cost bound stay sound.
    # OpenRouter provider prefs (same model, so cost-neutral) + prompt transforms.
    # NOT "models"/"route": those let a request fall back to a DIFFERENT (possibly
    # pricier) model than the one we priced, bypassing the cost bound.
    "provider", "transforms",
})


def _master() -> bytes:
    path = os.environ.get("MINT_MASTER_PATH", os.path.join(ROOT, "mint_master.hex"))
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_bytes(32).hex())
        os.chmod(path, 0o600)
    return bytes.fromhex(open(path).read().strip())


mint = Mint(_master())

# --- confetti channel lane (M4a: off-chain, in-memory referee) ---
# Flat price per request in credits; the channel pays this fixed delta per
# message (metered/variable channel pricing is M4b). Contract + Recipient are
# in-memory for M4a — a restart resets the registry and XMSS signer; persistence
# and an on-chain contract land in M4b.
CHANNEL_PRICE = int(os.environ.get("CHANNEL_PRICE", "50"))
# Payment-proof backend. "sp1" (default) = real SP1 STARK per payment: the
# proof hides the witness and the router verifies it cryptographically via the
# rpay binary. "clear" = ClearWitnessProver, the fast NON-ZK test double
# (witness travels in the clear) — tests and dev only.
CHANNEL_PROVER = os.environ.get("CHANNEL_PROVER", "sp1")
if CHANNEL_PROVER == "sp1":
    channel_prover = RealSP1Prover()
    if CHANNEL_LANE_ENABLED and not channel_prover.available():
        raise RuntimeError(
            f"CHANNEL_PROVER=sp1 but rpay binary missing at {channel_prover.bin_path}; "
            "build it (cd research/m4b-groth16 && cargo build --release --bin rpay) "
            "or set CHANNEL_PROVER=clear (dev only, not zero-knowledge)"
        )
elif CHANNEL_PROVER == "clear":
    channel_prover = ClearWitnessProver()
else:
    raise RuntimeError(f"CHANNEL_PROVER must be 'sp1' or 'clear', got {CHANNEL_PROVER!r}")
channel_contract = Contract(tau=int(os.environ.get("CHANNEL_TAU", "7")),
                            prover=channel_prover)
bob = Recipient(height=int(os.environ.get("CHANNEL_HEIGHT", "12")))
# Serializes countersigning (XMSS index) while verify runs off the event loop.
channel_accept_lock = threading.Lock()


def _channel_accept(m, price: int):
    with channel_accept_lock:
        return bob.accept(channel_contract, m, price=price)

db = sqlite3.connect(
    os.environ.get("STATE_DB_PATH", os.path.join(ROOT, "state.db")),
    check_same_thread=False,
)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute("CREATE TABLE IF NOT EXISTS spent(secret TEXT PRIMARY KEY)")
db.execute(
    "CREATE TABLE IF NOT EXISTS receipts("
    "id TEXT PRIMARY KEY, prepaid INT, cost INT, state TEXT, change_sigs TEXT, ts INTEGER)"
)
# Migrate older DBs (change_sigs = idempotent redemption; ts = crash-recovery
# age; res_day/res_usd = the daily-cap reservation to RELEASE if this receipt is
# crash-recovered, so a crash mid-request doesn't leak the reservation).
for _col, _type in (("change_sigs", "TEXT"), ("ts", "INTEGER"),
                    ("res_day", "TEXT"), ("res_usd", "REAL"),
                    ("change_key", "TEXT")):
    try:
        db.execute(f"ALTER TABLE receipts ADD COLUMN {_col} {_type}")
    except sqlite3.OperationalError:
        pass  # column already present
db.execute(
    "CREATE TABLE IF NOT EXISTS vouchers(code TEXT PRIMARY KEY, credits INT, state TEXT)"
)
# sigs/redeem_key = idempotent redemption: if a redeemed voucher's response is
# lost, the SAME blinded outputs (matched by redeem_key) get the cached sigs back
# instead of losing the voucher value; different outputs get a uniform 400 (so it
# is still not a probing oracle).
for _col in ("sigs", "redeem_key"):
    try:
        db.execute(f"ALTER TABLE vouchers ADD COLUMN {_col} TEXT")
    except sqlite3.OperationalError:
        pass
# Accounts store ONLY the key_hash, never the raw bearer key: a DB dump can't be
# used to spend or to link, and the account is a short-lived funding rendezvous
# (drained to ecash immediately), not a persistent identity. Migrate any older
# schema that kept the plaintext api_key column by rebuilding key_hash-only.
_acct_cols = [r[1] for r in db.execute("PRAGMA table_info(accounts)").fetchall()]
if _acct_cols and "api_key" in _acct_cols:
    db.execute("ALTER TABLE accounts RENAME TO _accounts_old")
    db.execute("CREATE TABLE accounts(key_hash TEXT PRIMARY KEY, balance INT)")
    db.execute("INSERT OR IGNORE INTO accounts(key_hash, balance) "
               "SELECT key_hash, balance FROM _accounts_old WHERE key_hash IS NOT NULL")
    db.execute("DROP TABLE _accounts_old")
else:
    db.execute("CREATE TABLE IF NOT EXISTS accounts(key_hash TEXT PRIMARY KEY, balance INT)")
db.execute(
    "CREATE TABLE IF NOT EXISTS seen_deposits(txhash TEXT PRIMARY KEY)"
)
# Idempotent claim records: a lost/retried claim returns the cached signatures
# instead of debiting the account a second time. `ts` lets a janitor expire them
# so the correlation surface (which account claimed when) does not persist.
db.execute(
    "CREATE TABLE IF NOT EXISTS claims(idem_key TEXT PRIMARY KEY, response TEXT, ts INTEGER)"
)
try:
    db.execute("ALTER TABLE claims ADD COLUMN ts INTEGER")
except sqlite3.OperationalError:
    pass
db.execute("CREATE TABLE IF NOT EXISTS spend_ledger(day TEXT PRIMARY KEY, usd REAL)")
db.commit()
db_write_lock = asyncio.Lock()

account_creations = deque()
account_rate_lock = threading.Lock()

# Simple custodial lane: deposit ETH -> credits on a bearer API key. This is the
# "simpler than OpenRouter" front door; the anonymous ecash/channel lanes are
# the trust-minimized alternative. CREDITS_PER_ETH sets the exchange rate.
CREDITS_PER_ETH = int(os.environ.get("CREDITS_PER_ETH", "10000000"))  # 1 ETH -> 10M credits
VAULT_ADDRESS = os.environ.get("VAULT_ADDRESS", "")
CONFETTI_ADDRESS = os.environ.get("CONFETTI_ADDRESS", "")  # on-chain escrow (M4b)
CHAIN_RPC = os.environ.get("CHAIN_RPC", "http://127.0.0.1:8545")
# Deposit-watcher supervision: the watcher writes a heartbeat every poll; a stale
# one means deposits are silently NOT being credited. /healthz surfaces this.
WATCHER_HEARTBEAT = os.environ.get("WATCHER_HEARTBEAT", "")
WATCHER_HALT = os.environ.get("WATCHER_HALT", "")
WATCHER_MAX_LAG_S = int(os.environ.get("WATCHER_MAX_LAG_S", "120"))


def _watcher_status():
    """Liveness of the deposit watcher for /healthz, or None if no on-chain
    deposit lane is configured. `ok` is False when the heartbeat is stale (dead/
    lagging watcher) or a reorg halt is latched — both mean deposits aren't being
    credited and need operator attention."""
    if not VAULT_ADDRESS:
        return None
    halted = bool(WATCHER_HALT and os.path.exists(WATCHER_HALT))
    s = {"configured": True, "alive": False, "halted": halted, "age_s": None}
    try:
        hb = json.load(open(WATCHER_HEARTBEAT))
        s["age_s"] = int(time.time()) - int(hb.get("ts", 0))
        s["head"], s["lag"] = hb.get("head"), hb.get("lag")
        s["alive"] = s["age_s"] <= WATCHER_MAX_LAG_S
    except Exception:
        pass  # no/unreadable heartbeat -> not alive
    s["ok"] = s["alive"] and not halted
    return s


app = FastAPI(title="anon-router")


@app.on_event("startup")
def log_safety_config():
    print(
        "anon-router SAFE config: "
        f"faucet={'on' if DEV_FAUCET else 'off'} "
        f"channel_lane={'on' if CHANNEL_LANE_ENABLED else 'off'} "
        f"channel_prover={CHANNEL_PROVER} "
        f"daily_cap_usd={DAILY_USD_CAP}"
    )


@app.on_event("startup")
async def start_receipt_recovery():
    # Awaited once before serving (immediate recovery of crashed receipts), then
    # periodically so a crash mid-run self-heals without needing a restart.
    await _recover_stale_receipts()

    async def loop():
        while True:
            await asyncio.sleep(60)
            try:
                await _recover_stale_receipts()
            except Exception as e:  # never let the sweep kill the loop
                print(f"receipt recovery sweep error: {e}")

    asyncio.create_task(loop())


@app.middleware("http")
async def privacy_headers(request: Request, call_next):
    """Transport-privacy hygiene (MVP): auth is stateless (headers only, no
    cookies/sessions), so nothing here links two requests. We never read
    X-Forwarded-For or log client IPs (run uvicorn with --no-access-log).
    Responses are no-store so intermediaries don't cache identifying data.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'"
    )
    return response  # Server header suppressed via uvicorn --no-server-header


@app.get("/")
def index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(ROOT, "web", "index.html"))


@app.get("/ecash.js")
def ecash_js():
    """In-browser BDHKE wallet module (same-origin; no CDN loads)."""
    from fastapi.responses import FileResponse
    return FileResponse(
        os.path.join(ROOT, "web", "ecash.js"), media_type="text/javascript"
    )


@app.get("/app.js")
def app_js():
    """Frontend app module (same-origin; no CDN loads, no inline script)."""
    from fastapi.responses import FileResponse
    return FileResponse(
        os.path.join(ROOT, "web", "app.js"), media_type="text/javascript"
    )


def _onion_address() -> str:
    """The published v3 onion, read from tor's own hostname file (the source of
    truth) so /privacy reflects reality whenever TOR_ONION=1 actually publishes
    a service; falls back to an explicit ONION_ADDRESS override for tests."""
    path = os.environ.get("ONION_HOSTNAME_FILE", "/data/tor_hs/hostname")
    try:
        with open(path) as f:
            addr = f.read().strip()
        if addr:
            return addr
    except OSError:
        pass
    return os.environ.get("ONION_ADDRESS", "")


@app.get("/privacy")
def privacy():
    """Machine-readable privacy posture — the honest boundary."""
    onion = _onion_address()
    return {
        "no_account": True,
        "no_card_no_kyc": True,
        "no_cookies_or_sessions": True,
        "auth": "stateless bearer key",
        "live_lane": "ecash (blind-signature, custodial)",
        "roadmap_lane": "confetti channel (non-custodial, per-request ZK)",
        "payment_unlinkable": ("cryptographically unlinkable at the signature layer "
                               "(blind-signed ecash); the custodial router still sees "
                               "deposit and redemption amounts + timing, so deposit a "
                               "common round amount and spend over time to avoid "
                               "statistical correlation"),
        "what_provider_sees": "only the router — not your identity, IP, or card",
        "what_router_sees": ("prompt content + connection metadata (IP/timing); IPs are "
                             "not logged, but requests under the same bearer key are "
                             "linkable by that key — rotate keys to unlink"),
        "what_the_model_sees": ("your prompt content — it must, to answer you; use a "
                                "local/self-hosted model to keep content off third parties"),
        "transport": {"onion_live": bool(onion), "onion": onion or None},
        "custody": ("custodial: the router holds prepaid balances — keep balances "
                    "small; the confetti lane removes this"),
        "funding": ("pseudonymous: the on-chain deposit is public — fund from a fresh "
                    "wallet, deposit a common round amount, and spend over time"),
        "not_yet_private": ["deposit funding origin (shielded pool on roadmap)"],
    }


def _parse_outputs(payload: dict) -> list[dict]:
    outputs = payload.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise HTTPException(400, "outputs required: [{amount, B_}]")
    for o in outputs:
        if int(o.get("amount", 0)) <= 0 or not o.get("B_"):
            raise HTTPException(400, "each output needs amount and B_")
    return outputs


def _sign_outputs(outputs: list[dict]) -> list[dict]:
    try:
        return [
            {"amount": int(o["amount"]), "C_": mint.sign_blinded(int(o["amount"]), o["B_"])}
            for o in outputs
        ]
    except ValueError as e:
        raise HTTPException(400, str(e))


def _verify_tokens(tokens: list[dict]) -> int:
    """Read-only: check MIN_PREPAY + every signature, return the prepaid total.
    Burns nothing — the caller bounds cost first, then spends atomically."""
    total = sum(int(t.get("amount", 0)) for t in tokens)
    if total < MIN_PREPAY:
        raise HTTPException(402, f"prepay {total} < minimum {MIN_PREPAY} credits")
    for t in tokens:
        if not mint.verify(int(t["amount"]), t["secret"], t["C"]):
            raise HTTPException(400, "invalid token signature")
    return total


async def _spend_and_open_receipt(tokens: list[dict], receipt_id: str,
                                  prepaid: int, res_day: str = None,
                                  res_usd: float = 0.0) -> None:
    """Burn the tokens AND open the pending receipt in ONE transaction, so a
    crash can never take payment without leaving a redeemable receipt. The
    daily-cap reservation (res_day/res_usd) is stored so crash recovery can
    release it."""
    async with db_write_lock:
        try:
            for t in tokens:
                db.execute("INSERT INTO spent(secret) VALUES (?)", (t["secret"],))
            db.execute(
                "INSERT INTO receipts(id, prepaid, cost, state, ts, res_day, res_usd) "
                "VALUES (?, ?, 0, 'pending', ?, ?, ?)",
                (receipt_id, prepaid, int(time.time()), res_day, res_usd),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            raise HTTPException(400, "token already spent")


async def _finalize(receipt_id: str, cost: int) -> None:
    async with db_write_lock:
        db.execute(
            "UPDATE receipts SET cost=?, state='final' WHERE id=? AND state='pending'",
            (cost, receipt_id),
        )
        db.commit()


def _receipt_id(tokens: list[dict]) -> str:
    """Deterministic receipt id = hash of the spent token secrets. Lets a client
    that lost the response recover its change in-band by re-presenting the same
    tokens — no separate, separately-timed redemption call to correlate. Uses a
    canonical JSON encoding of the sorted secrets (not a delimiter join) so a
    client can't craft secrets containing the delimiter to collide two different
    token sets onto one receipt id."""
    secrets_sorted = sorted(str(t.get("secret", "")) for t in tokens)
    return hashlib.sha256(json.dumps(secrets_sorted).encode()).hexdigest()


def _reject_non_text_content(body: dict) -> None:
    """Text-only MVP guard: a message `content` may be a string or a list of
    TEXT parts ({"type":"text",...}); any other part type (image_url, input_audio,
    file, ...) is rejected 400. This keeps `_bound_cost`'s length-based estimate a
    true upper bound on upstream cost (image/audio input costs are unrelated to
    the short reference length). Runs BEFORE any reserve/spend."""
    if not isinstance(body, dict):
        return
    for m in body.get("messages") or []:
        content = m.get("content") if isinstance(m, dict) else None
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise HTTPException(400, "unsupported message content (text only)")
        for part in content:
            if isinstance(part, str):
                continue
            if not isinstance(part, dict) or part.get("type") != "text":
                raise HTTPException(
                    400, "this router accepts text only; non-text content "
                    "(images/audio/files) is not supported")


def _blanks_key(blanks: list[dict]) -> str:
    """Fingerprint of the change blanks. The receipt binds to the FIRST blanks it
    settled; a recovery that presents DIFFERENT blanks (it can't unblind the
    already-issued change anyway) gets a clean 409 instead of usable-looking sigs
    it can't use. A legitimate client persists and re-sends identical blanks."""
    return hashlib.sha256(
        json.dumps([b.get("B_") for b in blanks]).encode()).hexdigest()


def _parse_change_blanks(request: Request) -> list[dict]:
    """Blinded 'blank' change outputs the client sends WITH the spend (Cashu
    NUT-08 style). Fixed count from the client, so the header size doesn't encode
    the change amount; the mint signs only the decompose(change) prefix."""
    hdr = request.headers.get("X-Cash-Change")
    if not hdr:
        raise HTTPException(400, "attach blinded change outputs in X-Cash-Change")
    if len(hdr) > 8192:  # bound decode work; 21 compressed points is ~1.5 KB b64
        raise HTTPException(400, "X-Cash-Change too large")
    try:
        blanks = json.loads(base64.b64decode(hdr))
    except Exception:
        raise HTTPException(400, "X-Cash-Change must be base64 JSON list of {B_}")
    # EXACTLY one blank per denomination: a fixed count so the header size never
    # encodes the change amount, and enough to cover any change of a spend capped
    # below 2^len (so _sign_change can never fail AFTER tokens are burned). Each
    # B_ must be a 33-byte compressed point in hex; validated here, before spend.
    if not isinstance(blanks, list) or len(blanks) != len(DENOMS):
        raise HTTPException(400, f"send exactly {len(DENOMS)} blinded change outputs")
    for b in blanks:
        if not isinstance(b, dict):
            raise HTTPException(400, "each change output must be an object")
        bp = b.get("B_")
        if not isinstance(bp, str) or len(bp) != 66 or bp[:2] not in ("02", "03"):
            raise HTTPException(400, "each B_ must be a 33-byte compressed point (hex)")
        # Curve-membership check NOW, before any spend: an off-curve point passes
        # the format check but makes mint.sign_blinded (-> ec.decompress) raise
        # LATER, after tokens are burned, leaving a pending receipt the sweep
        # would refund => free inference. Reject it here instead.
        try:
            ec.decompress(bytes.fromhex(bp))
        except ValueError:
            raise HTTPException(400, "each B_ must be a valid curve point")
    return blanks


def _sign_change(blanks: list[dict], change: int) -> list[dict]:
    """Sign the client's blank outputs for exactly `change` credits, assigning
    power-of-two denominations to the first blanks. In-band: the signatures ride
    back on the SAME response as the spend, so there is no separate change event."""
    denoms = decompose(change)
    if len(denoms) > len(blanks):
        raise HTTPException(400, f"need {len(denoms)} change outputs, got {len(blanks)}")
    return [{"amount": d, "C_": mint.sign_blinded(d, blanks[i]["B_"])}
            for i, d in enumerate(denoms)]


def _b64_change(change: int, cost: int, sigs: list[dict]) -> str:
    return base64.b64encode(
        json.dumps({"change": change, "cost": cost, "signatures": sigs}).encode()
    ).decode()


async def _settle_receipt(receipt_id: str, billed_cost: int, blanks: list[dict],
                          res_day: str = None, reserved_usd: float = 0.0,
                          actual_usd: float = 0.0) -> tuple[int, int, list[dict]]:
    """The SINGLE authoritative settlement + change-issuance point. Under one
    write lock, reads the receipt's current state and issues change EXACTLY once,
    always returning VALID signatures over the caller's `blanks`:

      pending  -> bill `billed_cost`, sign change, reconcile the daily-cap
                  reservation (winner only), cache + return.
      final    -> the stale-recovery sweep already refunded this to cost 0; sign
                  the FULL refund over these blanks and cache (the sweep already
                  released the reservation, so we don't). This is why a request
                  that raced the sweep still returns valid change, never the
                  empty sigs the sweep left.
      redeemed -> already settled; return the cached signatures (idempotent).

    Returns (change, cost, sigs). Every state transition is a GUARDED CAS
    (WHERE state='<expected>') and issuance/reconcile happen ONLY on rowcount==1,
    so change is issued once and the cap adjusted once even across MULTIPLE worker
    processes (SQLite serializes the conditional UPDATE; the in-process lock alone
    would not). On a lost CAS we re-read and return the winner's canonical sigs."""
    bk = _blanks_key(blanks)
    async with db_write_lock:
        row = db.execute(
            "SELECT prepaid, cost, state, change_sigs, change_key FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "not spent")
        prepaid, rcost, state, cached, ckey = row
        if state == "pending":
            billed = min(prepaid, billed_cost)
            change = prepaid - billed
            sigs = _sign_change(blanks, change) if change else []
            cur = db.execute(
                "UPDATE receipts SET cost=?, state='redeemed', change_sigs=?, change_key=? "
                "WHERE id=? AND state='pending'",
                (billed, json.dumps(sigs), bk, receipt_id))
            if cur.rowcount == 1:
                if res_day is not None:  # winner ALSO reconciles the cap, atomically
                    delta = float(actual_usd) - float(reserved_usd)
                    led = db.execute(
                        "UPDATE spend_ledger SET usd=MAX(0, usd+?) WHERE day=?",
                        (delta, res_day))
                    if led.rowcount == 0:
                        db.execute("INSERT INTO spend_ledger(day, usd) VALUES (?, ?)",
                                   (res_day, max(0.0, delta)))
                db.commit()
                return change, billed, sigs
            db.commit()  # lost CAS: fall through to return the winner's canonical
        elif state == "final":
            change = prepaid  # sweep refunded to cost 0 (reservation already released)
            sigs = _sign_change(blanks, change) if change else []
            cur = db.execute(
                "UPDATE receipts SET state='redeemed', change_sigs=?, change_key=? "
                "WHERE id=? AND state='final'",
                (json.dumps(sigs), bk, receipt_id))
            if cur.rowcount == 1:
                db.commit()
                return change, 0, sigs
            db.commit()  # lost CAS: fall through to canonical
        # Already 'redeemed' (or we lost a CAS): the change was issued ONCE, bound
        # to the first caller's blanks. Re-read and, if THIS caller's blanks don't
        # match, 409 rather than hand back signatures it cannot unblind (which the
        # client would otherwise absorb as unusable tokens).
        r2 = db.execute(
            "SELECT prepaid, cost, change_sigs, change_key FROM receipts WHERE id=?",
            (receipt_id,),
        ).fetchone()
        p2, c2, s2, k2 = r2
        if k2 is not None and k2 != bk:
            raise HTTPException(
                409, "change already issued to the original request's outputs")
        return max(0, p2 - (c2 or 0)), (c2 or 0), (json.loads(s2) if s2 else [])


async def _replay_change(receipt_id: str, blanks: list[dict]) -> JSONResponse:
    """Recovery entry point: return the change for an already-spent receipt
    in-band. 409 while the request is still in flight; otherwise delegate to the
    single settlement function, which issues change at most once and always over
    these blanks (final -> full refund, redeemed -> cached)."""
    row = db.execute("SELECT state FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "not spent")
    if row[0] == "pending":
        raise HTTPException(409, "request still in flight, retry shortly")
    change, cost, sigs = await _settle_receipt(receipt_id, 0, blanks)
    return JSONResponse({"change": change, "cost": cost, "signatures": sigs})


async def _recover_stale_receipts() -> int:
    """Finalize receipts left 'pending' by a crash (older than RECEIPT_STALE_SEC)
    to cost=0 so the payer redeems a full refund. The age guard means a live
    request's fresh receipt is never touched — safe even with multiple workers or
    a rolling restart. NULL ts (pre-migration receipts) are treated as stale."""
    async with db_write_lock:
        cutoff = int(time.time()) - RECEIPT_STALE_SEC
        stale = db.execute(
            "SELECT id, res_day, res_usd FROM receipts "
            "WHERE state='pending' AND (ts IS NULL OR ts < ?)",
            (cutoff,),
        ).fetchall()
        n = 0
        for _id, day, usd in stale:
            # Flip this receipt AND release its daily-cap reservation in the SAME
            # transaction, guarded by the CAS: only the worker that actually
            # transitions the receipt releases its reservation, so there is no
            # double-release across workers and no leak on a crash between the
            # two writes (they commit together). Inlined (not _reconcile_spend) to
            # avoid re-entering db_write_lock.
            cur = db.execute(
                "UPDATE receipts SET cost=0, state='final' "
                "WHERE id=? AND state='pending'",
                (_id,),
            )
            if cur.rowcount == 1:
                n += 1
                if day and usd:
                    db.execute(
                        "UPDATE spend_ledger SET usd=MAX(0, usd-?) WHERE day=?",
                        (float(usd), day),
                    )
        db.commit()
    if n:
        print(f"anon-router: recovered {n} stale receipt(s) -> full refund")
    return n


async def _reserve_daily_cap(estimate: float) -> tuple[str, float]:
    estimate = max(0.0, estimate)
    async with db_write_lock:
        day = db.execute("SELECT date('now')").fetchone()[0]
        if DAILY_USD_CAP <= 0:
            return day, 0.0
        row = db.execute(
            "SELECT usd FROM spend_ledger WHERE day=?", (day,)
        ).fetchone()
        today_usd = float(row[0]) if row else 0.0
        if today_usd + estimate > DAILY_USD_CAP:
            raise HTTPException(402, "daily budget reached, try later")
        db.execute(
            "INSERT INTO spend_ledger(day, usd) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET usd=usd+excluded.usd",
            (day, estimate),
        )
        db.commit()
    return day, estimate


async def _reconcile_spend(day: str, reserved_usd: float, actual_usd: float) -> None:
    delta = float(actual_usd) - reserved_usd
    async with db_write_lock:
        cur = db.execute(
            "UPDATE spend_ledger SET usd=MAX(0, usd+?) WHERE day=?",
            (delta, day),
        )
        if cur.rowcount == 0:
            db.execute(
                "INSERT INTO spend_ledger(day, usd) VALUES (?, ?)",
                (day, max(0.0, delta)),
            )
        db.commit()


def _num(x):
    """Coerce an upstream-supplied cost to a FINITE float, or None otherwise.
    A malformed `usage.cost` must NOT crash the billing path AFTER the spend
    (that would strand the receipt and free-ride the inference). Rejects non-
    numbers AND NaN/Infinity — `math.ceil(NaN)` raises and `float('inf')` would
    blow past the cap, both post-spend."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        f = float(x)  # a huge JSON int (e.g. 10**400) raises OverflowError here
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None  # NaN/inf, or a NEGATIVE cost (nonsensical) => treat as missing
    return f


def _billed_usd(cost_usd, credits: int) -> float:
    c = _num(cost_usd)
    return credits * CREDIT_USD if c is None else c


def _usd_to_credits(cost_usd) -> int:
    c = _num(cost_usd)
    if c is None:
        return 1  # missing/malformed usage; charge the floor, log for investigation
    return max(1, math.ceil(c * MARKUP / CREDIT_USD))


_pricing = {"data": {}, "ts": 0.0}


async def _model_price(model: str):
    """(prompt, completion) USD/token for `model` from OpenRouter, cached ~10 min.
    None if unknown/unavailable — the caller falls back to PRICE_CEIL_PER_TOKEN."""
    now = time.time()
    if not _pricing["data"] or now - _pricing["ts"] > 600:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{UPSTREAM}/models",
                                headers={"Authorization": f"Bearer {OPENROUTER_KEY}"})
            data = {}
            for m in r.json().get("data", []):
                p = m.get("pricing") or {}
                try:
                    data[m["id"]] = (float(p.get("prompt") or 0),
                                     float(p.get("completion") or 0))
                except (TypeError, ValueError):
                    pass
            if data:
                _pricing["data"], _pricing["ts"] = data, now
        except Exception:
            pass  # keep any stale cache; caller uses the ceiling fallback
    return _pricing["data"].get(model)


def _safe_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _est_input_tokens(body: dict) -> int:
    """Conservative UPPER bound on billable input tokens. Estimates over the
    ENTIRE forwarded body (already allowlist-filtered), not a hand-picked subset,
    so no client-controlled input-bearing field — messages, tools, functions,
    prediction, response_format's json_schema, stop, logit_bias, or any field
    added to the allowlist later — can slip past the cost bound. Rejects oversized
    input (413). Text/JSON only: image/audio token cost isn't modeled (text-chat
    MVP; non-text content parts are already rejected before this runs)."""
    try:
        blob = json.dumps(body)
    except Exception:
        blob = ""
    if len(blob) > MAX_INPUT_CHARS:
        raise HTTPException(413, "request input too large")
    return int(len(blob) / 3 * 1.4) + 16  # conservative chars->tokens (over-estimates)


async def _bound_cost(body: dict, upstream_model: str) -> tuple[float, int]:
    """Clamp output tokens on `body` in place, then compute the MAXIMUM this
    request can bill upstream (live per-model pricing, else a true ceiling).
    Reject if that maximum exceeds the per-request cap. Returns (worst_usd,
    worst_credits) — what a lane must have prepaid/reserved to never be under-paid.
    """
    # Canonicalize the output limit across BOTH field names to one clamped field,
    # so neither can be used to request more output than we price.
    req_out = max(_safe_int(body.get("max_tokens"), 0),
                  _safe_int(body.get("max_completion_tokens"), 0))
    out_cap = min(req_out if req_out > 0 else MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS)
    body["max_tokens"] = out_cap
    body.pop("max_completion_tokens", None)
    n = max(1, min(_safe_int(body.get("n"), 1), 8))
    if "n" in body:
        body["n"] = n
    out = out_cap * n
    inp = _est_input_tokens(body)
    price = await _model_price(upstream_model)
    p_price, c_price = price if price is not None else (PRICE_CEIL_PER_TOKEN,
                                                        PRICE_CEIL_PER_TOKEN)
    worst = inp * p_price + out * c_price
    if worst > MAX_REQUEST_USD:
        raise HTTPException(402, f"request may cost ${worst:.4f} upstream, over the "
                            f"${MAX_REQUEST_USD:.2f} per-request cap — lower max_tokens/n")
    return worst, max(1, math.ceil(worst / CREDIT_USD))


@app.get("/mint/keys")
def keys():
    return {
        "pubkeys": mint.pubkeys(),
        "credit_usd": CREDIT_USD,
        "min_prepay": MIN_PREPAY,
        "markup": MARKUP,
    }


@app.get("/healthz")
def healthz():
    out = {
        "status": "ok",
        "faucet": DEV_FAUCET,
        "channel_lane": CHANNEL_LANE_ENABLED,
        "daily_cap_usd": DAILY_USD_CAP,
    }
    watcher = _watcher_status()
    if watcher is not None:
        out["watcher"] = watcher
        # Router itself is serving (existing balances + ecash still work), but
        # flag deposit-crediting as degraded so monitoring can alert.
        if not watcher["ok"]:
            out["degraded"] = "deposit watcher stale or halted"
    return out


@app.post("/mint/topup")
async def topup(request: Request):
    """Dev faucet: issues credits for free. Replaced by the USDC deposit watcher."""
    if not DEV_FAUCET:
        raise HTTPException(403, "faucet disabled; fund via USDC deposit")
    outputs = _parse_outputs(await request.json())
    total = sum(int(o["amount"]) for o in outputs)
    if total > FAUCET_MAX:
        raise HTTPException(400, f"faucet cap {FAUCET_MAX} credits per call")
    return {"signatures": _sign_outputs(outputs)}


def _key_hash(api_key: str) -> str:
    from web3 import Web3
    return "0x" + Web3.keccak(text=api_key).hex()  # 0x-prefixed to match watcher event


@app.post("/account/new")
async def account_new(request: Request):
    """Mint a fresh bearer API key. Fund it by depositing ETH to the vault
    referencing its key_hash; the watcher credits it."""
    now = time.monotonic()
    with account_rate_lock:
        while account_creations and account_creations[0] <= now - 60:
            account_creations.popleft()
        if len(account_creations) >= ACCOUNT_RATE_PER_MIN:
            raise HTTPException(429, "account creation rate limit reached")
        account_creations.append(now)
    api_key = "sk-anon-" + secrets.token_urlsafe(24)
    kh = _key_hash(api_key)
    async with db_write_lock:
        db.execute(
            "INSERT INTO accounts(key_hash, balance) VALUES (?, 0)", (kh,),
        )
        db.commit()
    from web3 import Web3
    public_base_url = os.environ.get("PUBLIC_BASE_URL")
    if not public_base_url:
        host = request.headers.get("Host", "127.0.0.1:8402")
        forwarded_proto = request.headers.get("X-Forwarded-Proto")
        hostname = urlparse(f"//{host}").hostname
        scheme = (
            forwarded_proto.split(",", 1)[0].strip()
            if forwarded_proto
            else "http" if hostname in {"localhost", "127.0.0.1", "::1"} else "https"
        )
        public_base_url = f"{scheme}://{host}"
    return {
        "api_key": api_key,
        "key_hash": kh,
        "vault_address": VAULT_ADDRESS,
        "deposit_selector": "0x" + Web3.keccak(text="deposit(bytes32)").hex()[:8],
        "credits_per_eth": CREDITS_PER_ETH,
        "credit_usd": CREDIT_USD,
        "base_url": public_base_url + "/v1",
    }


def _account_balance(api_key: str):
    if not api_key:
        return None
    row = db.execute(
        "SELECT balance FROM accounts WHERE key_hash=?", (_key_hash(api_key),)
    ).fetchone()
    return None if row is None else row[0]


@app.get("/account/status")
def account_status(request: Request):
    auth = request.headers.get("Authorization", "")
    key = auth[7:] if auth.startswith("Bearer ") else ""
    bal = _account_balance(key)
    if bal is None:
        raise HTTPException(401, "unknown API key")
    return {"balance": bal, "credit_usd": CREDIT_USD, "usd": bal * CREDIT_USD}


@app.post("/account/credit")
async def account_credit(request: Request):
    """Internal: the deposit watcher credits an account for an on-chain deposit.
    Guarded by CREDIT_SECRET so only the watcher can call it."""
    secret = os.environ.get("CREDIT_SECRET", "")
    if not secret or not hmac.compare_digest(
        request.headers.get("X-Credit-Secret", ""), secret
    ):
        raise HTTPException(403, "forbidden")
    body = await request.json()
    kh, credits, txhash = body["key_hash"], int(body["credits"]), body["txhash"]
    # dedup per LOG, not per tx: one tx can emit several Deposited events
    event_id = f"{txhash}:{body.get('log_index', 0)}"
    async with db_write_lock:
        cur = db.cursor()
        if cur.execute(
            "SELECT 1 FROM seen_deposits WHERE txhash=?", (event_id,)
        ).fetchone():
            return {"status": "already_credited"}  # idempotent per deposit event
        cur.execute(
            "UPDATE accounts SET balance=balance+? WHERE key_hash=?", (credits, kh)
        )
        if cur.rowcount == 0:
            db.rollback()
            # do NOT mark seen: the account may be created later and re-scanned
            return {"status": "no_such_account"}
        cur.execute("INSERT INTO seen_deposits(txhash) VALUES (?)", (event_id,))
        db.commit()
    return {"status": "credited", "credits": credits}


@app.get("/config")
def config():
    """Frontend config: on-chain addresses + function selectors (computed
    server-side so the browser needs no keccak/ABI library)."""
    from web3 import Web3

    def sel(sig):
        return "0x" + Web3.keccak(text=sig).hex()[:8]

    return {
        "vault_address": VAULT_ADDRESS,
        "confetti_address": CONFETTI_ADDRESS,
        "router_pk_B": bob.pk_B.hex(),
        "credits_per_eth": CREDITS_PER_ETH,
        "channel_price": CHANNEL_PRICE,
        "selectors": {
            "vaultDeposit": sel("deposit(bytes32)"),
            "open": sel("open(bytes16,address,bytes32,bytes32)"),
            "closeGenesis": sel("closeGenesis(bytes16,bytes32,bytes)"),
            "finalize": sel("finalize(bytes16)"),
            "withdraw": sel("withdraw()"),
            "withdrawTo": sel("withdrawTo(address)"),
            "channels": sel("channels(bytes16)"),
            "withdrawable": sel("withdrawable(address)"),
        },
    }


@app.post("/mint/claim")
async def mint_claim(request: Request):
    """Convert a deposited account balance into unlinkable ecash. The depositor
    proves ownership with the api_key (its keyHash was named in the deposit),
    submits blinded outputs, and the mint blind-signs them. Spending those
    tokens (X-Cash lane) is cryptographically unlinkable to this deposit — this
    is the payment-private path (funding is linkable; use is not)."""
    auth = request.headers.get("Authorization", "")
    key = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    outputs = _parse_outputs(await request.json())
    total = sum(int(o["amount"]) for o in outputs)
    signatures = _sign_outputs(outputs)

    # Idempotency is MANDATORY: a claim debits the account, so a lost-response
    # retry MUST carry the same Idempotency-Key to get the cached signatures back
    # instead of debiting a second time. Reject a claim without one rather than
    # allow a silent double-debit. Keyed under the account so one key can't replay
    # another's claim.
    idem = request.headers.get("Idempotency-Key")
    if not idem:
        raise HTTPException(400, "Idempotency-Key header required for /mint/claim")
    idem_key = f"{_key_hash(key)}:{idem}"
    async with db_write_lock:
        try:
            if idem_key:
                row = db.execute(
                    "SELECT response FROM claims WHERE idem_key=?", (idem_key,)
                ).fetchone()
                if row:
                    if row[0]:
                        return json.loads(row[0])
                    raise HTTPException(
                        409, "claim with this Idempotency-Key is in progress"
                    )
                db.execute(
                    "INSERT INTO claims(idem_key, response, ts) VALUES (?, '', ?)",
                    (idem_key, int(time.time())),
                )
            bal = _account_balance(key)
            if bal is None:
                raise HTTPException(401, "unknown API key")
            if total > bal:
                raise HTTPException(402, f"claim {total} exceeds balance {bal}")

            # Signing is pure, so failures happen before the debit. The debit and
            # completed idempotency response are then committed atomically.
            cur = db.execute(
                "UPDATE accounts SET balance=balance-? WHERE key_hash=? AND balance>=?",
                (total, _key_hash(key), total),
            )
            if cur.rowcount == 0:
                raise HTTPException(409, "balance changed, retry")
            response = {"signatures": signatures}
            if idem_key:
                db.execute(
                    "UPDATE claims SET response=? WHERE idem_key=?",
                    (json.dumps(response), idem_key),
                )
            db.commit()
            return response
        except Exception:
            db.rollback()
            raise


@app.get("/channel/params")
def channel_params():
    """Everything a client needs to open a confetti channel."""
    return {
        "pk_B": bob.pk_B.hex(),
        "root": channel_contract.root().hex(),
        "price_per_request": CHANNEL_PRICE,
        "credit_usd": CREDIT_USD,
        "tau_days": channel_contract.tau,
        # which payment-proof backend this router accepts: "sp1" (real STARK,
        # witness-hiding; pi is ~3.7 MB so send it in the `_channel_payment`
        # body field, not the header) or "clear" (dev test double).
        "prover": CHANNEL_PROVER,
        # XMSS tree height of this router's countersigning key. Genesis
        # payments size their DUMMY signed-branch auth path to this so first
        # payments stay shape-identical to later (signed-parent) ones.
        "xmss_height": bob.xmss.height,
    }


@app.post("/channel/open")
async def channel_open(request: Request):
    if not CHANNEL_LANE_ENABLED:
        raise HTTPException(503, "channel lane disabled (no on-chain escrow wired)")
    body = await request.json()
    try:
        cid = bytes.fromhex(body["cid"])
        D = int(body["D"])
        C_open = bytes.fromhex(body["C_open"])
    except (KeyError, ValueError):
        raise HTTPException(400, "need cid (hex), D (int), C_open (hex)")
    if cid in channel_contract.channels:
        raise HTTPException(400, "cid already opened")
    rec = ChannelRecord(cid, D, bob.pk_B, C_open)
    idx = channel_contract.open(rec)
    path = channel_contract.registry.proof(idx)
    return {
        "rec_index": idx,
        "rec_path": [p.hex() for p in path],
        "root": channel_contract.root().hex(),
    }


@app.post("/mint/redeem")
async def redeem_voucher(request: Request):
    """Redeem a voucher into ecash. The code travels in the REQUEST BODY (never
    the URL, so it can't leak via logs/history/referer) and there is no
    voucher-status GET endpoint (an unauthenticated status oracle would let
    anyone probe a code's existence/value/redemption state). A wrong code and an
    already-redeemed code return the same 400 so the endpoint isn't an oracle."""
    body = await request.json()
    code = body.get("code")
    if not isinstance(code, str) or not code:
        raise HTTPException(400, "code required in body")
    outputs = _parse_outputs(body)
    if any(int(o["amount"]) not in DENOMS for o in outputs):
        raise HTTPException(400, "output amounts must be valid denominations")
    # Fingerprint of the exact blinded outputs: gates idempotent recovery to the
    # original client (same blinds) without becoming a code-probing oracle.
    rk = hashlib.sha256(json.dumps(
        sorted((int(o["amount"]), o["B_"]) for o in outputs)).encode()).hexdigest()
    async with db_write_lock:
        row = db.execute(
            "SELECT credits, state, sigs, redeem_key FROM vouchers WHERE code=?", (code,)
        ).fetchone()
        credits = row[0] if row else None
        if not row or sum(int(o["amount"]) for o in outputs) != credits:
            raise HTTPException(400, "invalid or already-redeemed voucher")
        if row[1] == "redeemed":
            # Idempotent replay ONLY for the identical blinds; anything else 400s.
            if row[3] == rk:
                return {"credits": credits,
                        "signatures": json.loads(row[2]) if row[2] else []}
            raise HTTPException(400, "invalid or already-redeemed voucher")
        # Sign FIRST (pure, may raise 400 on a bad blinded point) so a signing
        # failure can't burn the voucher; then CAS to redeemed, caching sigs +
        # blinds fingerprint so a lost response is recoverable.
        signatures = _sign_outputs(outputs)
        cur = db.execute(
            "UPDATE vouchers SET state='redeemed', sigs=?, redeem_key=? "
            "WHERE code=? AND state='issued'",
            (json.dumps(signatures), rk, code),
        )
        db.commit()
        if cur.rowcount != 1:  # lost the race: return the winner's cached sigs
            won = db.execute(
                "SELECT sigs, redeem_key FROM vouchers WHERE code=?", (code,)
            ).fetchone()
            if won and won[1] == rk:
                return {"credits": credits,
                        "signatures": json.loads(won[0]) if won[0] else []}
            raise HTTPException(400, "invalid or already-redeemed voucher")
    return {"credits": credits, "signatures": signatures}


@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{UPSTREAM}/models")
        data = r.json().get("data", [])
        for prefix, u in UPSTREAMS.items():
            try:
                lr = await client.get(f"{u['base']}/models", timeout=5)
                for m in lr.json().get("data", []):
                    data.insert(0, {"id": f"{prefix}/{m['id']}", "object": "model", "free": u["free"]})
            except httpx.HTTPError:
                pass  # free upstream down: paid lane still works
    return {"object": "list", "data": data}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    # Channel payments too large for a header (the SP1 STARK proof is ~3.7 MB
    # base64) ride in this reserved body field. Pop it unconditionally so no
    # lane ever forwards payment material upstream.
    channel_payment_body = (
        body.pop("_channel_payment", None) if isinstance(body, dict) else None
    )
    # Recovery FIRST, before any lane routing: an X-Cash-Recover request only
    # reclaims change for already-spent tokens. Handling it here guarantees it can
    # never run inference — not the free (local/*) lane, not the channel lane, not
    # the paid lane — regardless of the model or any payment header it carries.
    if request.headers.get("X-Cash-Recover"):
        cash_header = request.headers.get("X-Cash")
        if not cash_header:
            raise HTTPException(400, "X-Cash-Recover requires X-Cash")
        try:
            tokens = json.loads(base64.b64decode(cash_header))
        except Exception:
            raise HTTPException(400, "X-Cash must be base64 JSON token list")
        blanks = _parse_change_blanks(request)
        rid = _receipt_id(tokens)
        if db.execute("SELECT 1 FROM receipts WHERE id=?", (rid,)).fetchone() is None:
            raise HTTPException(404, "not spent")
        return await _replay_change(rid, blanks)
    # Privacy core: the upstream inference provider must only ever see "the
    # router" — never who paid or which session. Forward ONLY known inference
    # parameters (strict allowlist), dropping every client-supplied identity /
    # session / tracking / retention field — user, metadata, store, session_id,
    # trace, and anything we don't recognize. An allowlist (vs a denylist) means
    # a new provider-side tracking field can't leak by default. Payment material
    # rides in headers / the popped field above, never in the forwarded body.
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k in UPSTREAM_ALLOWED_FIELDS}
    # Text-MVP cost-bound integrity: `_bound_cost` prices input by serialized
    # length, which is NOT a conservative bound for multimodal parts (a short
    # `image_url` can trigger large upstream image-processing cost). Reject any
    # non-text content part BEFORE reserving/spending, so worst-case pricing is
    # always an upper bound. (Removing modalities/audio only covered the output.)
    _reject_non_text_content(body)
    base, key, free, upstream_model = resolve_route(str(body.get("model", "")))
    body["model"] = upstream_model
    upstream_headers = {"Content-Type": "application/json"}
    if key:
        upstream_headers["Authorization"] = f"Bearer {key}"
    url = f"{base}/chat/completions"

    if free:
        if body.get("stream"):

            async def gen_free():
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(STREAM_READ_TIMEOUT_S, connect=15.0)) as client:
                    async with client.stream(
                        "POST", url, json=body, headers=upstream_headers
                    ) as r:
                        async for line in r.aiter_lines():
                            yield line + "\n"

            return StreamingResponse(
                gen_free(),
                media_type="text/event-stream",
                headers={"X-Cost-Credits": "0"},
            )
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, json=body, headers=upstream_headers)
        return JSONResponse(
            r.json(), status_code=r.status_code, headers={"X-Cost-Credits": "0"}
        )

    # NOTE: there is deliberately NO "pay for inference with the account key"
    # lane. The bearer account is a funding rendezvous only — deposit/voucher ->
    # /mint/claim -> unlinkable ecash. Paying inference directly from the account
    # would tie every request to one persistent, re-linkable identifier, which is
    # exactly the property this product exists to avoid. Inference is paid ONLY
    # with blind-signed ecash (X-Cash) or a confetti channel.
    channel_header = request.headers.get("X-Channel-Payment")
    if channel_header or channel_payment_body is not None:
        if not CHANNEL_LANE_ENABLED:
            raise HTTPException(503, "channel lane disabled (no on-chain escrow wired)")
        try:
            payment_j = (
                json.loads(base64.b64decode(channel_header))
                if channel_header
                else channel_payment_body
            )
            m = payment_from_j(payment_j)
        except Exception:
            raise HTTPException(
                400,
                "channel payment must be base64 JSON in X-Channel-Payment "
                "or a JSON object in the _channel_payment body field",
            )
        # The channel pays a flat CHANNEL_PRICE, so reject any request whose
        # bounded max cost exceeds it (else the operator eats the difference).
        worst_usd, worst_credits = await _bound_cost(body, upstream_model)
        if worst_credits > CHANNEL_PRICE:
            raise HTTPException(402, f"request may cost {worst_credits} credits, over the "
                                f"channel's flat price {CHANNEL_PRICE}; lower max_tokens/n")
        reservation_day, reserved_usd = await _reserve_daily_cap(worst_usd)
        try:
            # STARK verification shells out to the rpay binary (~0.5 s); run it
            # off the event loop so other lanes stay responsive.
            sigma = await asyncio.to_thread(_channel_accept, m, CHANNEL_PRICE)
        except ValueError as e:
            await _reconcile_spend(reservation_day, reserved_usd, 0.0)
            raise HTTPException(402, f"channel payment rejected: {e}")
        countersign = base64.b64encode(json.dumps(sig_to_j(sigma)).encode()).decode()
        body["usage"] = {"include": True}
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                r = await client.post(url, json=body, headers=upstream_headers)
        except Exception:
            await _reconcile_spend(
                reservation_day,
                reserved_usd,
                max(_billed_usd(None, CHANNEL_PRICE), reserved_usd),
            )
            raise
        try:
            data = r.json()
            usage = data.get("usage") if isinstance(data, dict) else None
        except Exception:
            # Malformed body: don't 500 (which would skip the reconcile and leak
            # the reservation). Reconcile conservatively and return the raw text.
            data, usage = {"error": "malformed upstream response"}, None
        cost_usd = (usage.get("cost") if isinstance(usage, dict) else None) \
            if r.status_code == 200 else None
        await _reconcile_spend(
            reservation_day,
            reserved_usd,
            max(_billed_usd(cost_usd, CHANNEL_PRICE), reserved_usd)
            if cost_usd is None
            else _billed_usd(cost_usd, CHANNEL_PRICE),
        )
        return JSONResponse(
            data,
            status_code=r.status_code,
            headers={"X-Channel-Countersign": countersign,
                     "X-Cost-Credits": str(CHANNEL_PRICE)},
        )

    cash_header = request.headers.get("X-Cash")
    if not cash_header:
        raise HTTPException(402, "payment required: attach tokens in X-Cash or X-Channel-Payment header")
    try:
        tokens = json.loads(base64.b64decode(cash_header))
    except Exception:
        raise HTTPException(400, "X-Cash must be base64 JSON token list")
    change_blanks = _parse_change_blanks(request)
    receipt_id = _receipt_id(tokens)

    # A genuine retry of a real request (same tokens, no recover header) replays
    # the change instead of re-running (and double-charging) the inference.
    # (Explicit X-Cash-Recover is handled earlier, before any lane routing.)
    existing = db.execute(
        "SELECT 1 FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    if existing is not None:
        return await _replay_change(receipt_id, change_blanks)

    # Bound the worst-case upstream cost and require the prepay covers it BEFORE
    # burning anything — so the router can never be billed more than was paid.
    prepaid = _verify_tokens(tokens)
    # Cap the prepay so change always decomposes into <= len(DENOMS) outputs (the
    # fixed blank count). A legitimate client over-selects by at most one token
    # (< 2^(len-1)); this only rejects absurd over-prepay, and does so before any
    # spend, so change signing can never fail after tokens are burned.
    if prepaid >= (1 << len(DENOMS)):
        raise HTTPException(402, "prepay too large; attach fewer/smaller tokens")
    worst_usd, worst_credits = await _bound_cost(body, upstream_model)
    if prepaid < worst_credits:
        raise HTTPException(402, f"prepay {prepaid} < {worst_credits} credits needed to "
                            "cover this request's maximum cost; attach more tokens")

    reservation_day, reserved_usd = await _reserve_daily_cap(worst_usd)
    try:
        await _spend_and_open_receipt(tokens, receipt_id, prepaid,
                                      reservation_day, reserved_usd)
    except HTTPException:
        # Tokens spent between our check and now: fall back to the replay path.
        await _reconcile_spend(reservation_day, reserved_usd, 0.0)
        row = db.execute(
            "SELECT 1 FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        if row is not None:
            return await _replay_change(receipt_id, change_blanks)
        raise

    body["usage"] = {"include": True}  # OpenRouter returns exact USD cost

    if body.get("stream"):

        async def gen():
            state = {"cost_usd": None, "produced": False, "done": False,
                     "upstream_failed": False}

            async def _settle():
                # MUST run even on a mid-stream client disconnect (GeneratorExit /
                # CancelledError are BaseException, so callers put this in
                # `finally`). `done` is set only AFTER the settle COMMITS, so a
                # cancellation mid-commit still lets the `finally` reschedule it —
                # otherwise the receipt could stay pending and be stale-refunded
                # (free inference). Idempotent via _settle_receipt's state read.
                if state["done"]:
                    return None
                if state["upstream_failed"] or not state["produced"]:
                    cost = 0  # upstream error / no output delivered: full refund
                elif _num(state["cost_usd"]) is not None:
                    cost = _usd_to_credits(state["cost_usd"])
                else:
                    # Output WAS delivered but the upstream gave no usable cost:
                    # charge the pre-reserved BOUNDED WORST case (never the 1-credit
                    # floor, which would be near-free inference), refund the rest.
                    cost = worst_credits
                billed = min(prepaid, cost)
                actual_usd = (max(_billed_usd(state["cost_usd"], billed), reserved_usd)
                              if state["cost_usd"] is None
                              else _billed_usd(state["cost_usd"], billed))
                change, fcost, sigs = await _settle_receipt(
                    receipt_id, billed, change_blanks,
                    reservation_day, reserved_usd, actual_usd)
                state["done"] = True
                return {"change": change, "cost": fcost, "signatures": sigs}

            settled = None
            stream_start = time.monotonic()
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(STREAM_READ_TIMEOUT_S, connect=15.0)) as client:
                    # Bound the header/connect phase by the total budget too: a
                    # trickling-headers upstream must not outlive the stale sweep.
                    _req = client.build_request(
                        "POST", url, json=body, headers=upstream_headers)
                    try:
                        r = await asyncio.wait_for(
                            client.send(_req, stream=True), timeout=MAX_STREAM_TOTAL_SEC)
                    except Exception:
                        state["upstream_failed"] = True
                        r = None
                    if r is None:
                        yield "data: " + json.dumps({"error": "upstream unavailable"}) + "\n\n"
                    else:
                      try:
                        if r.status_code != 200:
                            # Upstream error: full refund (not a min-charge), and
                            # surface the error to the client as one SSE frame.
                            # Bound the body read by the remaining total budget so a
                            # trickling error body can't outlive the stale sweep.
                            state["upstream_failed"] = True
                            try:
                                remaining = max(1.0, MAX_STREAM_TOTAL_SEC
                                                - (time.monotonic() - stream_start))
                                err = await asyncio.wait_for(r.aread(), timeout=remaining)
                            except Exception:
                                err = b""
                            try:
                                emsg = json.loads(err)
                            except Exception:
                                emsg = {"error": err.decode("utf-8", "ignore") or "upstream error"}
                            yield "data: " + json.dumps(emsg) + "\n\n"
                        else:
                          _it = r.aiter_lines()
                          while True:
                            remaining = MAX_STREAM_TOTAL_SEC - (time.monotonic() - stream_start)
                            if remaining <= 0:
                                break  # total ceiling: never outlast the stale sweep
                            try:
                                # Per-read wait bounds a trickling upstream that never
                                # emits a newline (which would else defeat both the
                                # read timeout and the total-age check).
                                line = await asyncio.wait_for(
                                    _it.__anext__(),
                                    timeout=min(remaining, STREAM_READ_TIMEOUT_S))
                            except (StopAsyncIteration, asyncio.TimeoutError):
                                break
                            if line.startswith("data: "):
                                if line[6:] == "[DONE]":
                                    continue  # suppress upstream DONE; we send our
                                    # own AFTER the change event so clients that
                                    # stop at [DONE] don't miss the change
                                try:
                                    chunk = json.loads(line[6:])
                                except json.JSONDecodeError:
                                    chunk = None
                                if isinstance(chunk, dict):
                                    # Cost extraction and the content scan are
                                    # INDEPENDENT: a malformed `usage` must never
                                    # suppress detecting that paid output was
                                    # delivered (else we'd bill the 1-credit floor
                                    # for a full response = near-free inference).
                                    usage = chunk.get("usage")
                                    if isinstance(usage, dict):
                                        # Normalize at store: a malformed/non-finite
                                        # cost becomes semantically "missing" (None)
                                        # so settlement's produced-output branch
                                        # charges the bounded worst case, never the
                                        # 1-credit floor (near-free inference).
                                        _c = _num(usage.get("cost"))
                                        if _c is not None:
                                            state["cost_usd"] = _c
                                    for choice in chunk.get("choices") or []:
                                        delta = (choice.get("delta")
                                                 if isinstance(choice, dict) else None) or {}
                                        if (
                                            delta.get("content")
                                            or delta.get("reasoning")
                                            or delta.get("tool_calls")
                                            or delta.get("function_call")
                                            or delta.get("audio")  # defense: audio
                                            or (isinstance(choice, dict) and choice.get("text"))
                                        ):
                                            state["produced"] = True
                                yield line + "\n"
                            elif line.startswith("event:"):
                                continue  # never relay upstream SSE event lines
                                # (defends against a spoofed x-cash-change event)
                            else:
                                yield line + "\n"  # keep-alive comments / blanks
                      finally:
                        await r.aclose()
                # Clean completion: settle, deliver change as the final event,
                # THEN emit our own [DONE] so a client that stops at [DONE] has
                # already seen the change.
                settled = await _settle()
                if settled:
                    yield ("event: x-cash-change\ndata: "
                           + json.dumps(settled) + "\n\n")
                yield "data: [DONE]\n\n"
            finally:
                # Covers disconnect/cancel/upstream-error: always finalize the
                # spend (billed), so no path leaves a burned token unfinalized
                # (which the stale sweep would later refund => free inference).
                # Run detached so it completes even though THIS task is being
                # cancelled; _settle is idempotent via state["done"]. The client
                # recovers the cached change via X-Cash-Recover.
                if not state["done"]:
                    asyncio.ensure_future(_settle())

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _refund(status: int, body_obj: dict):
        # Any failure AFTER the spend -> full refund, delivered in-band on the
        # same response (never a bare error, which would strand the tokens).
        change, cost, sigs = await _settle_receipt(
            receipt_id, 0, change_blanks, reservation_day, reserved_usd, 0.0)
        return JSONResponse(body_obj, status_code=status,
                            headers={"X-Cash-Change": _b64_change(change, cost, sigs)})

    try:
        # Total wall-clock ceiling (not just httpx's per-read inactivity timeout):
        # a trickling upstream must not outlive RECEIPT_STALE_SEC, or the sweep
        # would finalize this receipt and the settle below would race it.
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            r = await asyncio.wait_for(
                client.post(url, json=body, headers=upstream_headers),
                timeout=MAX_STREAM_TOTAL_SEC)
    except Exception:
        return await _refund(502, {"error": "upstream request failed or timed out"})
    if r.status_code != 200:
        try:
            body_obj = (r.json() if r.headers.get("content-type", "").startswith("application/json")
                        else {"error": r.text})
        except Exception:
            body_obj = {"error": "upstream error"}
        return await _refund(r.status_code, body_obj)
    try:
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("upstream body is not an object")
        usage = data.get("usage")
        cost_usd = usage.get("cost") if isinstance(usage, dict) else None
    except Exception:
        # Malformed 200 body AFTER the spend (non-JSON, non-object, or a
        # wrong-typed `usage`): refund rather than 500 (a bare 500 here would
        # leave the receipt pending and strand the client's tokens).
        return await _refund(502, {"error": "malformed upstream response"})
    if _num(cost_usd) is not None:
        cost = min(prepaid, _usd_to_credits(cost_usd))
        actual_usd = _billed_usd(cost_usd, cost)
    else:
        # A 200 delivered output but the cost is unknown/malformed: charge the
        # pre-reserved BOUNDED WORST case (never the 1-credit floor => near-free),
        # refund the over-prepay. `worst_credits` is the up-front cost bound.
        cost = min(prepaid, worst_credits)
        actual_usd = max(_billed_usd(None, cost), reserved_usd)
    change, fcost, sigs = await _settle_receipt(
        receipt_id, cost, change_blanks, reservation_day, reserved_usd, actual_usd)
    return JSONResponse(
        data,
        headers={"X-Cost-Credits": str(fcost),
                 "X-Cash-Change": _b64_change(change, fcost, sigs)},
    )
