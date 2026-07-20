"""anon-router: payer-anonymous OpenAI-compatible inference proxy.

Payment: prepaid blind-signature tokens (see mint.py). The server cannot link
a request to the deposit that funded it. Overpayment comes back as blind
change via a one-time receipt.

Run: uvicorn server:app --host 127.0.0.1 --port 8402
"""
import asyncio
import base64
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

from mint import DENOMS, Mint
from confetti.chain import ChannelRecord
from confetti.channel import Contract, Recipient
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
channel_contract = Contract(tau=int(os.environ.get("CHANNEL_TAU", "7")))
bob = Recipient(height=int(os.environ.get("CHANNEL_HEIGHT", "12")))

db = sqlite3.connect(
    os.environ.get("STATE_DB_PATH", os.path.join(ROOT, "state.db")),
    check_same_thread=False,
)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute("CREATE TABLE IF NOT EXISTS spent(secret TEXT PRIMARY KEY)")
db.execute(
    "CREATE TABLE IF NOT EXISTS receipts("
    "id TEXT PRIMARY KEY, prepaid INT, cost INT, state TEXT)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS vouchers(code TEXT PRIMARY KEY, credits INT, state TEXT)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS accounts("
    "api_key TEXT PRIMARY KEY, key_hash TEXT UNIQUE, balance INT)"
)
db.execute(
    "CREATE TABLE IF NOT EXISTS seen_deposits(txhash TEXT PRIMARY KEY)"
)
# Idempotent claim records: a lost/retried claim returns the cached signatures
# instead of debiting the account a second time.
db.execute(
    "CREATE TABLE IF NOT EXISTS claims(idem_key TEXT PRIMARY KEY, response TEXT)"
)
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

app = FastAPI(title="anon-router")


@app.on_event("startup")
def log_safety_config():
    print(
        "anon-router SAFE config: "
        f"faucet={'on' if DEV_FAUCET else 'off'} "
        f"channel_lane={'on' if CHANNEL_LANE_ENABLED else 'off'} "
        f"daily_cap_usd={DAILY_USD_CAP}"
    )


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
        "style-src 'self' 'unsafe-inline'; script-src 'self' "
        "'sha256-gkyHUsRdzdPu6KtWzlGmD+4VazcOqAZeFwmXlLvwU9A='"
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


@app.get("/privacy")
def privacy():
    """Machine-readable privacy posture — the honest boundary."""
    return {
        "no_account": True,
        "no_cookies_or_sessions": True,
        "auth": "stateless bearer key or per-request channel payment",
        "payment_unlinkable": "channel lane (confetti) + ecash lane",
        "what_provider_sees": "only the router, never the end user",
        "what_router_sees": "prompt content + connection metadata (IP/timing)",
        "not_yet_private": ["transport (use Tor/proxy)", "deposit funding origin"],
        "upgrade_path": ["onion service", "shielded-pool funding", "key rotation"],
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


async def _spend(tokens: list[dict]) -> int:
    total = sum(int(t.get("amount", 0)) for t in tokens)
    if total < MIN_PREPAY:
        raise HTTPException(402, f"prepay {total} < minimum {MIN_PREPAY} credits")
    for t in tokens:
        if not mint.verify(int(t["amount"]), t["secret"], t["C"]):
            raise HTTPException(400, "invalid token signature")
    async with db_write_lock:
        cur = db.cursor()
        try:
            for t in tokens:
                cur.execute("INSERT INTO spent(secret) VALUES (?)", (t["secret"],))
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            raise HTTPException(400, "token already spent")
    return total


async def _finalize(receipt_id: str, cost: int) -> None:
    async with db_write_lock:
        db.execute(
            "UPDATE receipts SET cost=?, state='final' WHERE id=? AND state='pending'",
            (cost, receipt_id),
        )
        db.commit()


async def _reserve_daily_cap() -> tuple[str, float]:
    estimate = max(0.0, MAX_REQUEST_USD)
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


def _billed_usd(cost_usd: float | None, credits: int) -> float:
    return credits * CREDIT_USD if cost_usd is None else float(cost_usd)


def _usd_to_credits(cost_usd: float | None) -> int:
    if cost_usd is None:
        return 1  # upstream gave no usage; charge the floor, log for investigation
    return max(1, math.ceil(cost_usd * MARKUP / CREDIT_USD))


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
    return {
        "status": "ok",
        "faucet": DEV_FAUCET,
        "channel_lane": CHANNEL_LANE_ENABLED,
        "daily_cap_usd": DAILY_USD_CAP,
    }


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
            "INSERT INTO accounts(api_key, key_hash, balance) VALUES (?, ?, 0)",
            (api_key, kh),
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
    row = db.execute("SELECT balance FROM accounts WHERE api_key=?", (api_key,)).fetchone()
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

    # Idempotency: a client retrying a claim (lost response, timeout) sends the
    # same Idempotency-Key and gets the cached signatures back, never a second
    # debit. Keyed under the account so one key can't replay another's claim.
    idem = request.headers.get("Idempotency-Key")
    idem_key = f"{_key_hash(key)}:{idem}" if idem else None
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
                    "INSERT INTO claims(idem_key, response) VALUES (?, '')",
                    (idem_key,),
                )
            bal = _account_balance(key)
            if bal is None:
                raise HTTPException(401, "unknown API key")
            if total > bal:
                raise HTTPException(402, f"claim {total} exceeds balance {bal}")

            # Signing is pure, so failures happen before the debit. The debit and
            # completed idempotency response are then committed atomically.
            cur = db.execute(
                "UPDATE accounts SET balance=balance-? WHERE api_key=? AND balance>=?",
                (total, key, total),
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


@app.get("/mint/voucher/{code}")
def voucher_info(code: str):
    row = db.execute(
        "SELECT credits, state FROM vouchers WHERE code=?", (code,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "unknown voucher")
    return {"credits": row[0], "state": row[1]}


@app.post("/mint/voucher/{code}")
async def redeem_voucher(code: str, request: Request):
    outputs = _parse_outputs(await request.json())
    if any(int(o["amount"]) not in DENOMS for o in outputs):
        raise HTTPException(400, "output amounts must be valid denominations")
    signatures = _sign_outputs(outputs)
    async with db_write_lock:
        row = db.execute(
            "SELECT credits, state FROM vouchers WHERE code=?", (code,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "unknown voucher")
        credits = row[0]
        if sum(int(o["amount"]) for o in outputs) != credits:
            raise HTTPException(400, f"outputs must sum to {credits} credits")
        # Mark redeemed atomically so a concurrent redeem can't double-issue.
        cur = db.execute(
            "UPDATE vouchers SET state='redeemed' WHERE code=? AND state='issued'",
            (code,),
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(400, "voucher already redeemed")
    return {"credits": credits, "signatures": signatures}


@app.get("/mint/change/{receipt_id}")
def change_info(receipt_id: str):
    row = db.execute(
        "SELECT prepaid, cost, state FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "unknown receipt")
    prepaid, cost, state = row
    change = max(0, prepaid - (cost or 0)) if state != "pending" else None
    return {"state": state, "prepaid": prepaid, "cost": cost, "change": change}


@app.post("/mint/change/{receipt_id}")
async def redeem_change(receipt_id: str, request: Request):
    raw_body = await request.body()
    async with db_write_lock:
        row = db.execute(
            "SELECT prepaid, cost, state FROM receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "unknown receipt")
        prepaid, cost, state = row
        if state == "pending":
            raise HTTPException(409, "request still in flight, retry shortly")
        if state != "final":
            raise HTTPException(409, "already redeemed or not final")
        change = max(0, prepaid - cost)
        if change:
            outputs = _parse_outputs(json.loads(raw_body))
            if sum(int(o["amount"]) for o in outputs) != change:
                raise HTTPException(400, f"outputs must sum to change of {change} credits")
            signatures = _sign_outputs(outputs)
        else:
            signatures = []
        cur = db.execute(
            "UPDATE receipts SET state='redeemed' WHERE id=? AND state='final'",
            (receipt_id,),
        )
        db.commit()
        if cur.rowcount == 0:
            raise HTTPException(409, "already redeemed or not final")
    return {"change": change, "cost": cost, "signatures": signatures}


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
    base, key, free, upstream_model = resolve_route(str(body.get("model", "")))
    body["model"] = upstream_model
    upstream_headers = {"Content-Type": "application/json"}
    if key:
        upstream_headers["Authorization"] = f"Bearer {key}"
    url = f"{base}/chat/completions"

    if free:
        if body.get("stream"):

            async def gen_free():
                async with httpx.AsyncClient(timeout=None) as client:
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

    # Bearer-key account lane: works with any OpenAI-compatible client.
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer sk-anon-"):
        key = auth[len("Bearer "):]
        bal = _account_balance(key)
        if bal is None:
            raise HTTPException(401, "unknown API key")
        if bal <= 0:
            raise HTTPException(402, "insufficient credits; deposit ETH to top up")
        reservation_day, reserved_usd = await _reserve_daily_cap()
        async with db_write_lock:
            cur = db.execute(
                "UPDATE accounts SET balance=balance-? WHERE api_key=? AND balance>=?",
                (bal, key, bal),
            )
            db.commit()
        if cur.rowcount == 0:
            await _reconcile_spend(reservation_day, reserved_usd, 0.0)
            raise HTTPException(402, "insufficient credits; deposit ETH to top up")
        body["usage"] = {"include": True}
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                r = await client.post(url, json=body, headers=upstream_headers)
        except Exception:
            async with db_write_lock:
                db.execute(
                    "UPDATE accounts SET balance=balance+? WHERE api_key=?", (bal, key)
                )
                db.commit()
            await _reconcile_spend(reservation_day, reserved_usd, 0.0)
            raise
        if r.status_code != 200:
            async with db_write_lock:
                db.execute(
                    "UPDATE accounts SET balance=balance+? WHERE api_key=?", (bal, key)
                )
                db.commit()
            await _reconcile_spend(reservation_day, reserved_usd, 0.0)
            return JSONResponse(
                r.json()
                if r.headers.get("content-type", "").startswith("application/json")
                else {"error": r.text},
                status_code=r.status_code,
            )
        data = r.json()
        cost_usd = (data.get("usage") or {}).get("cost")
        cost = min(bal, _usd_to_credits(cost_usd))
        async with db_write_lock:
            db.execute(
                "UPDATE accounts SET balance=balance+? WHERE api_key=?", (bal - cost, key)
            )
            db.commit()
        actual_usd = _billed_usd(cost_usd, cost)
        if cost_usd is None:
            actual_usd = max(actual_usd, reserved_usd)
        await _reconcile_spend(reservation_day, reserved_usd, actual_usd)
        return JSONResponse(
            data,
            headers={"X-Cost-Credits": str(cost), "X-Balance": str(max(0, bal - cost))},
        )

    channel_header = request.headers.get("X-Channel-Payment")
    if channel_header:
        if not CHANNEL_LANE_ENABLED:
            raise HTTPException(503, "channel lane disabled (no on-chain escrow wired)")
        try:
            m = payment_from_j(json.loads(base64.b64decode(channel_header)))
        except Exception:
            raise HTTPException(400, "X-Channel-Payment must be base64 JSON")
        reservation_day, reserved_usd = await _reserve_daily_cap()
        try:
            sigma = bob.accept(channel_contract, m, price=CHANNEL_PRICE)
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
        data = r.json()
        cost_usd = (data.get("usage") or {}).get("cost") if r.status_code == 200 else None
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

    reservation_day, reserved_usd = await _reserve_daily_cap()
    try:
        prepaid = await _spend(tokens)
    except Exception:
        await _reconcile_spend(reservation_day, reserved_usd, 0.0)
        raise
    receipt_id = secrets.token_hex(16)
    async with db_write_lock:
        db.execute(
            "INSERT INTO receipts(id, prepaid, cost, state) "
            "VALUES (?, ?, 0, 'pending')",
            (receipt_id, prepaid),
        )
        db.commit()

    body["usage"] = {"include": True}  # OpenRouter returns exact USD cost

    if body.get("stream"):

        async def gen():
            cost_usd = None
            produced_output = False
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST", url, json=body, headers=upstream_headers
                    ) as r:
                        async for line in r.aiter_lines():
                            if line.startswith("data: ") and line[6:] != "[DONE]":
                                try:
                                    chunk = json.loads(line[6:])
                                    usage = chunk.get("usage")
                                    if usage and usage.get("cost") is not None:
                                        cost_usd = usage["cost"]
                                    for choice in chunk.get("choices") or []:
                                        delta = choice.get("delta") or {}
                                        if (
                                            delta.get("content")
                                            or delta.get("reasoning")
                                            or delta.get("tool_calls")
                                            or delta.get("function_call")
                                            or choice.get("text")
                                        ):
                                            produced_output = True
                                except (json.JSONDecodeError, AttributeError):
                                    pass
                            yield line + "\n"
            finally:
                cost = (
                    prepaid
                    if cost_usd is None and produced_output
                    else _usd_to_credits(cost_usd)
                )
                billed = min(prepaid, cost)
                # A disconnect after output keeps the full prepay; this can
                # over-charge on a network flake, but avoids free inference.
                await _finalize(receipt_id, billed)
                await _reconcile_spend(
                    reservation_day,
                    reserved_usd,
                    max(_billed_usd(cost_usd, billed), reserved_usd)
                    if cost_usd is None
                    else _billed_usd(cost_usd, billed),
                )

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"X-Change-Receipt": receipt_id},
        )

    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, json=body, headers=upstream_headers)
    except Exception:
        await _finalize(receipt_id, 0)
        await _reconcile_spend(reservation_day, reserved_usd, 0.0)
        raise
    if r.status_code != 200:
        await _finalize(receipt_id, 0)  # upstream failed: full refund via change
        await _reconcile_spend(reservation_day, reserved_usd, 0.0)
        return JSONResponse(
            r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text},
            status_code=r.status_code,
            headers={"X-Change-Receipt": receipt_id},
        )
    data = r.json()
    cost_usd = (data.get("usage") or {}).get("cost")
    cost = min(prepaid, _usd_to_credits(cost_usd))
    await _finalize(receipt_id, cost)
    actual_usd = _billed_usd(cost_usd, cost)
    if cost_usd is None:
        actual_usd = max(actual_usd, reserved_usd)
    await _reconcile_spend(reservation_day, reserved_usd, actual_usd)
    return JSONResponse(
        data,
        headers={"X-Change-Receipt": receipt_id, "X-Cost-Credits": str(cost)},
    )
