"""anon-router: payer-anonymous OpenAI-compatible inference proxy.

Payment: prepaid blind-signature tokens (see mint.py). The server cannot link
a request to the deposit that funded it. Overpayment comes back as blind
change via a one-time receipt.

Run: uvicorn server:app --host 127.0.0.1 --port 8402
"""
import base64
import json
import math
import os
import secrets
import sqlite3

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

OPENROUTER_KEY = os.environ["OPENROUTER_API_KEY"]
UPSTREAM = os.environ.get("UPSTREAM", "https://openrouter.ai/api/v1")
CREDIT_USD = float(os.environ.get("CREDIT_USD", "0.0001"))  # 1 credit = $0.0001
MARKUP = float(os.environ.get("MARKUP", "1.0"))
MIN_PREPAY = int(os.environ.get("MIN_PREPAY", "500"))  # credits required up front
DEV_FAUCET = os.environ.get("DEV_FAUCET", "1") == "1"
FAUCET_MAX = int(os.environ.get("FAUCET_MAX", "500000"))  # per topup call, dev only

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
    path = os.path.join(ROOT, "mint_master.hex")
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

db = sqlite3.connect(os.path.join(ROOT, "state.db"), check_same_thread=False)
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
db.commit()

# Simple custodial lane: deposit ETH -> credits on a bearer API key. This is the
# "simpler than OpenRouter" front door; the anonymous ecash/channel lanes are
# the trust-minimized alternative. CREDITS_PER_ETH sets the exchange rate.
CREDITS_PER_ETH = int(os.environ.get("CREDITS_PER_ETH", "10000000"))  # 1 ETH -> 10M credits
VAULT_ADDRESS = os.environ.get("VAULT_ADDRESS", "")
CONFETTI_ADDRESS = os.environ.get("CONFETTI_ADDRESS", "")  # on-chain escrow (M4b)
CHAIN_RPC = os.environ.get("CHAIN_RPC", "http://127.0.0.1:8545")

app = FastAPI(title="anon-router")


@app.get("/")
def index():
    from fastapi.responses import FileResponse
    return FileResponse(os.path.join(ROOT, "web", "index.html"))


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


def _spend(tokens: list[dict]) -> int:
    total = sum(int(t.get("amount", 0)) for t in tokens)
    if total < MIN_PREPAY:
        raise HTTPException(402, f"prepay {total} < minimum {MIN_PREPAY} credits")
    for t in tokens:
        if not mint.verify(int(t["amount"]), t["secret"], t["C"]):
            raise HTTPException(400, "invalid token signature")
    cur = db.cursor()
    try:
        for t in tokens:
            cur.execute("INSERT INTO spent(secret) VALUES (?)", (t["secret"],))
    except sqlite3.IntegrityError:
        db.rollback()
        raise HTTPException(400, "token already spent")
    db.commit()
    return total


def _finalize(receipt_id: str, cost: int) -> None:
    db.execute(
        "UPDATE receipts SET cost=?, state='final' WHERE id=? AND state='pending'",
        (cost, receipt_id),
    )
    db.commit()


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
def account_new():
    """Mint a fresh bearer API key. Fund it by depositing ETH to the vault
    referencing its key_hash; the watcher credits it."""
    api_key = "sk-anon-" + secrets.token_urlsafe(24)
    kh = _key_hash(api_key)
    db.execute(
        "INSERT INTO accounts(api_key, key_hash, balance) VALUES (?, ?, 0)",
        (api_key, kh),
    )
    db.commit()
    from web3 import Web3
    return {
        "api_key": api_key,
        "key_hash": kh,
        "vault_address": VAULT_ADDRESS,
        "deposit_selector": "0x" + Web3.keccak(text="deposit(bytes32)").hex()[:8],
        "credits_per_eth": CREDITS_PER_ETH,
        "credit_usd": CREDIT_USD,
        "base_url": os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8402") + "/v1",
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
    if not secret or request.headers.get("X-Credit-Secret") != secret:
        raise HTTPException(403, "forbidden")
    body = await request.json()
    kh, credits, txhash = body["key_hash"], int(body["credits"]), body["txhash"]
    cur = db.cursor()
    try:
        cur.execute("INSERT INTO seen_deposits(txhash) VALUES (?)", (txhash,))
    except sqlite3.IntegrityError:
        return {"status": "already_credited"}  # idempotent per tx
    cur.execute(
        "UPDATE accounts SET balance=balance+? WHERE key_hash=?", (credits, kh)
    )
    db.commit()
    if cur.rowcount == 0:
        return {"status": "no_such_account"}
    return {"status": "credited", "credits": credits}


@app.get("/config")
def config():
    """Frontend config: on-chain addresses + function selectors (computed
    server-side so the browser needs no keccak/ABI library)."""
    from web3 import Web3

    def sel(sig):
        return "0x" + Web3.keccak(text=sig).hex()[:8]

    return {
        "rpc": CHAIN_RPC,
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
    row = db.execute(
        "SELECT credits, state FROM vouchers WHERE code=?", (code,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "unknown voucher")
    credits = row[0]
    outputs = _parse_outputs(await request.json())
    if any(int(o["amount"]) not in DENOMS for o in outputs):
        raise HTTPException(400, "output amounts must be valid denominations")
    if sum(int(o["amount"]) for o in outputs) != credits:
        raise HTTPException(400, f"outputs must sum to {credits} credits")
    # mark redeemed atomically before signing so a concurrent redeem can't double-issue
    cur = db.execute(
        "UPDATE vouchers SET state='redeemed' WHERE code=? AND state='issued'", (code,)
    )
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(400, "voucher already redeemed")
    return {"credits": credits, "signatures": _sign_outputs(outputs)}


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
    row = db.execute(
        "SELECT prepaid, cost, state FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "unknown receipt")
    prepaid, cost, state = row
    if state == "pending":
        raise HTTPException(409, "request still in flight, retry shortly")
    if state == "redeemed":
        raise HTTPException(400, "receipt already redeemed")
    change = max(0, prepaid - cost)
    if change == 0:
        db.execute("UPDATE receipts SET state='redeemed' WHERE id=?", (receipt_id,))
        db.commit()
        return {"change": 0, "cost": cost, "signatures": []}
    outputs = _parse_outputs(await request.json())
    if sum(int(o["amount"]) for o in outputs) != change:
        raise HTTPException(400, f"outputs must sum to change of {change} credits")
    signatures = _sign_outputs(outputs)
    db.execute("UPDATE receipts SET state='redeemed' WHERE id=?", (receipt_id,))
    db.commit()
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
        body["usage"] = {"include": True}
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, json=body, headers=upstream_headers)
        if r.status_code != 200:
            return JSONResponse(r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text}, status_code=r.status_code)
        data = r.json()
        cost = _usd_to_credits((data.get("usage") or {}).get("cost"))
        db.execute("UPDATE accounts SET balance=balance-? WHERE api_key=?", (cost, key))
        db.commit()
        return JSONResponse(
            data,
            headers={"X-Cost-Credits": str(cost), "X-Balance": str(max(0, bal - cost))},
        )

    channel_header = request.headers.get("X-Channel-Payment")
    if channel_header:
        try:
            m = payment_from_j(json.loads(base64.b64decode(channel_header)))
        except Exception:
            raise HTTPException(400, "X-Channel-Payment must be base64 JSON")
        try:
            sigma = bob.accept(channel_contract, m, price=CHANNEL_PRICE)
        except ValueError as e:
            raise HTTPException(402, f"channel payment rejected: {e}")
        countersign = base64.b64encode(json.dumps(sig_to_j(sigma)).encode()).decode()
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(url, json=body, headers=upstream_headers)
        return JSONResponse(
            r.json(),
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

    prepaid = _spend(tokens)
    receipt_id = secrets.token_hex(16)
    db.execute(
        "INSERT INTO receipts(id, prepaid, cost, state) VALUES (?, ?, 0, 'pending')",
        (receipt_id, prepaid),
    )
    db.commit()

    body["usage"] = {"include": True}  # OpenRouter returns exact USD cost

    if body.get("stream"):

        async def gen():
            cost_usd = None
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "POST", url, json=body, headers=upstream_headers
                    ) as r:
                        async for line in r.aiter_lines():
                            if line.startswith("data: ") and line[6:] != "[DONE]":
                                try:
                                    usage = json.loads(line[6:]).get("usage")
                                    if usage and usage.get("cost") is not None:
                                        cost_usd = usage["cost"]
                                except (json.JSONDecodeError, AttributeError):
                                    pass
                            yield line + "\n"
            finally:
                _finalize(receipt_id, min(prepaid, _usd_to_credits(cost_usd)))

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"X-Change-Receipt": receipt_id},
        )

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(url, json=body, headers=upstream_headers)
    if r.status_code != 200:
        _finalize(receipt_id, 0)  # upstream failed: full refund via change
        return JSONResponse(
            r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text},
            status_code=r.status_code,
            headers={"X-Change-Receipt": receipt_id},
        )
    data = r.json()
    cost_usd = (data.get("usage") or {}).get("cost")
    cost = min(prepaid, _usd_to_credits(cost_usd))
    _finalize(receipt_id, cost)
    return JSONResponse(
        data,
        headers={"X-Change-Receipt": receipt_id, "X-Cost-Credits": str(cost)},
    )
