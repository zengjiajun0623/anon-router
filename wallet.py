"""Client wallet: holds blind-signed tokens, pays per request, redeems change."""
from __future__ import annotations  # PEP 563: keep `X | None` hints lazy so the
# client runs on Python 3.9 (older laptops), not just 3.10+.
import base64
import json
import os
import pickle
import secrets
import sys
import time

import httpx

from confetti.channel import Payer
from confetti.sp1 import RealSP1Prover
from confetti.wire import payment_to_j, sig_from_j
from mint import blind, decompose, unblind

# Default to the hosted service so the CLI works out of the box; override with
# ANON_ROUTER_URL (or --url) to point at a local/self-hosted router.
DEFAULT_MINT = os.environ.get(
    "ANON_ROUTER_URL", "https://anon-router-production.up.railway.app")
WALLET_PATH = os.path.expanduser("~/.anon-router/wallet.json")
CHANNEL_PATH = os.path.expanduser("~/.anon-router/channel.pkl")
# A payment proved ahead of time (during the previous reply's think-time) so the
# next chat message spends instantly instead of blocking ~45s on the prover.
PREPARED_PATH = CHANNEL_PATH + ".prepared"


class Wallet:
    def __init__(self, mint_url: str = DEFAULT_MINT, path: str = WALLET_PATH,
                 tor: bool = False):
        self.url = mint_url.rstrip("/")
        self.path = path
        # tor: route everything through the local Tor SOCKS proxy so requests
        # reach the .onion over Tor (the router never sees a client IP).
        proxy = "socks5h://127.0.0.1:9050" if tor else None
        self.http = httpx.Client(timeout=300, proxy=proxy)
        self._load()
        self._keys = None

    def _load(self) -> None:
        if os.path.exists(self.path):
            data = json.load(open(self.path))
            self.tokens = data.get("tokens", [])
            self.account = data.get("account")   # {"api_key", "key_hash"} or None
        else:
            self.tokens = []
            self.account = None

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"mint": self.url, "tokens": self.tokens,
                       "account": self.account}, f, indent=1)
        os.chmod(self.path, 0o600)

    # ---- account (on-chain-funded) lane ----

    def new_account(self) -> dict:
        """Mint an anonymous bearer key. Fund it with deposit_onchain(); the
        router's watcher credits it, then claim_from_account() converts the
        balance into unlinkable ecash."""
        r = self.http.post(f"{self.url}/account/new")
        r.raise_for_status()
        data = r.json()
        self.account = {"api_key": data["api_key"], "key_hash": data["key_hash"]}
        self._save()
        return data

    def account_status(self) -> dict:
        if not self.account:
            raise RuntimeError("no account; run: cli.py account")
        r = self.http.get(f"{self.url}/account/status",
                          headers={"Authorization": f"Bearer {self.account['api_key']}"})
        r.raise_for_status()
        return r.json()

    def deposit_onchain(self, eth: float, funding_key_hex: str, rpc: str) -> dict:
        """Sign + broadcast CreditVault.deposit(key_hash) from a local funding
        key and wait for the tx to mine. The watcher then credits the account;
        poll account_status() for the balance. Deposit → spend is unlinkable:
        the deposit funds an account, which you drain into blind-signed ecash."""
        from web3 import Web3
        if not self.account:
            self.new_account()
        cfg = self.http.get(f"{self.url}/config").json()
        vault, cpe = cfg["vault_address"], cfg["credits_per_eth"]
        if not vault:
            raise RuntimeError("router has no vault_address configured")
        w3 = Web3(Web3.HTTPProvider(rpc))
        signer = w3.eth.account.from_key(funding_key_hex)
        kh = self.account["key_hash"]
        vault_c = w3.eth.contract(
            address=Web3.to_checksum_address(vault),
            abi=[{"inputs": [{"name": "keyHash", "type": "bytes32"}],
                  "name": "deposit", "outputs": [], "stateMutability": "payable",
                  "type": "function"}])
        # Bump gas above the current estimate with a floor so the tx gets mined
        # promptly even when the RPC reports a very low quiet-period gas price
        # (a too-low price can leave the tx pending indefinitely on Sepolia).
        gas_price = max(int(w3.eth.gas_price * 1.5), w3.to_wei(2, "gwei"))
        tx = vault_c.functions.deposit(bytes.fromhex(kh[2:])).build_transaction({
            "from": signer.address, "value": w3.to_wei(eth, "ether"),
            "nonce": w3.eth.get_transaction_count(signer.address, "pending"),
            "gas": 100000, "gasPrice": gas_price})
        txh = w3.eth.send_raw_transaction(signer.sign_transaction(tx).raw_transaction)
        rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=420)
        h = txh.hex()
        tx_str = h if h.startswith("0x") else "0x" + h
        if rcpt.status != 1:
            raise RuntimeError(f"deposit tx {tx_str} reverted on-chain")
        return {"tx": tx_str, "expected_credits": int(eth * cpe)}

    def keys(self) -> dict:
        if self._keys is None:
            self._keys = self.http.get(f"{self.url}/mint/keys").json()
        return self._keys

    def balance(self) -> int:
        return sum(t["amount"] for t in self.tokens)

    def _request_signatures(self, endpoint: str, amount: int,
                            headers: dict | None = None) -> list[dict]:
        """Blind fresh secrets for `amount`, post to endpoint, unblind, store."""
        blinds = []
        for denom in decompose(amount):
            secret = secrets.token_hex(32)
            blinded_hex, r = blind(secret)
            blinds.append({"amount": denom, "secret": secret, "r": r, "B_": blinded_hex})
        body = {"outputs": [{"amount": b["amount"], "B_": b["B_"]} for b in blinds]}
        # Stable idempotency key + one retry: if the response is lost in transit,
        # the retry returns the same signatures rather than debiting twice.
        hdrs = dict(headers or {})
        hdrs.setdefault("Idempotency-Key", secrets.token_hex(16))
        for attempt in range(2):
            try:
                resp = self.http.post(f"{self.url}{endpoint}", json=body, headers=hdrs)
                break
            except httpx.TransportError:
                if attempt == 1:
                    raise
        resp.raise_for_status()
        pubkeys = self.keys()["pubkeys"]
        minted = []
        for b, sig in zip(blinds, resp.json()["signatures"]):
            c_hex = unblind(sig["C_"], b["r"], pubkeys[str(b["amount"])])
            minted.append({"amount": b["amount"], "secret": b["secret"], "C": c_hex})
        self.tokens.extend(minted)
        self._save()
        return minted

    def topup(self, credits: int) -> int:
        self._request_signatures("/mint/topup", credits)
        return self.balance()

    def claim_from_account(self, api_key: str, amount: int) -> int:
        """Convert `amount` of a deposited account balance into unlinkable ecash
        tokens held in this wallet. Spending them later is unlinkable to the
        on-chain deposit that funded the account."""
        self._request_signatures("/mint/claim", amount,
                                  headers={"Authorization": f"Bearer {api_key}"})
        return self.balance()

    def redeem_voucher(self, code: str) -> int:
        info = self.http.get(f"{self.url}/mint/voucher/{code}")
        info.raise_for_status()
        data = info.json()
        if data["state"] != "issued":
            raise RuntimeError("voucher already redeemed")
        self._request_signatures(f"/mint/voucher/{code}", data["credits"])
        return self.balance()

    def _select(self, amount: int) -> list[dict]:
        """Pop tokens covering >= amount, largest first (change comes back)."""
        chosen, total = [], 0
        for t in sorted(self.tokens, key=lambda t: -t["amount"]):
            if total >= amount:
                break
            chosen.append(t)
            total += t["amount"]
        if total < amount:
            raise RuntimeError(f"insufficient balance: have {self.balance()}, need {amount}")
        for t in chosen:
            self.tokens.remove(t)
        self._save()
        return chosen

    def redeem_change(self, receipt_id: str, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while True:
            info = self.http.get(f"{self.url}/mint/change/{receipt_id}").json()
            if info["state"] != "pending":
                break
            if time.time() > deadline:
                raise RuntimeError(f"receipt {receipt_id} still pending")
            time.sleep(0.5)
        change = info["change"]
        if change and info["state"] == "final":
            self._request_signatures(f"/mint/change/{receipt_id}", change)
        return {"cost": info["cost"], "change": change}

    # ---- confetti channel lane ----

    def channel_open(self, deposit: int) -> dict:
        """Open a confetti channel with the router funded by `deposit` credits.
        Persists the Payer locally (demo persistence; see channel.py notes).

        The payment-proof backend follows the router: "sp1" means every payment
        carries a real SP1 STARK (witness-hiding, ~1 min native proving);
        "clear" is the dev test double (witness in the clear, NOT anonymous)."""
        params = self.http.get(f"{self.url}/channel/params").json()
        prover = (
            RealSP1Prover(xmss_height=int(params.get("xmss_height", 12)))
            if params.get("prover", "clear") == "sp1" else None
        )
        if prover is not None and not prover.available():
            raise RuntimeError(
                f"router requires SP1 payment proofs but the rpay prover binary "
                f"is missing at {prover.bin_path} — build it with: "
                "cd research/m4b-groth16 && cargo build --release --bin rpay")
        payer = Payer(deposit, bytes.fromhex(params["pk_B"]), prover)
        resp = self.http.post(
            f"{self.url}/channel/open",
            json={"cid": payer.cid.hex(), "D": deposit,
                  "C_open": payer.C_open.hex()},
        )
        resp.raise_for_status()
        data = resp.json()
        payer.register(data["rec_index"],
                       [bytes.fromhex(p) for p in data["rec_path"]],
                       bytes.fromhex(data["root"]))
        os.makedirs(os.path.dirname(CHANNEL_PATH), exist_ok=True)
        with open(CHANNEL_PATH, "wb") as f:
            pickle.dump(payer, f)
        os.chmod(CHANNEL_PATH, 0o600)
        return {"deposit": deposit, "price": params["price_per_request"]}

    def _load_channel(self) -> Payer:
        if not os.path.exists(CHANNEL_PATH):
            raise RuntimeError("no channel open; run: cli.py channel open <credits>")
        with open(CHANNEL_PATH, "rb") as f:
            return pickle.load(f)

    def _save_channel(self, payer: Payer) -> None:
        with open(CHANNEL_PATH, "wb") as f:
            pickle.dump(payer, f)

    def channel_status(self) -> dict:
        payer = self._load_channel()
        return {"deposit": payer.D, "spent": payer.tip.bal,
                "remaining": payer.D - payer.tip.bal, "payments": payer.tip.index}

    def _channel_params(self) -> dict:
        return self.http.get(f"{self.url}/channel/params").json()

    def _channel_require_prover(self, payer: Payer, params: dict) -> str:
        router_prover = params.get("prover", "clear")
        payer_prover = "sp1" if isinstance(payer.prover, RealSP1Prover) else "clear"
        if router_prover != payer_prover:
            raise RuntimeError(
                f"channel was opened with the {payer_prover!r} prover but the "
                f"router now requires {router_prover!r}; open a new channel")
        return payer_prover

    # ---- pipelined proving (prove payment N+1 during reply N's think-time) ----
    #
    # A confetti payment costs ~45s of client-side STARK proving. Doing that on
    # the critical path means every message waits ~45s before inference starts.
    # Instead we split proving from spending: after each message settles we prove
    # the *next* payment in the background while the user reads the reply, so the
    # next message spends an already-proven token instantly. Only the very first
    # message of a fresh channel pays the full latency; the rest is hidden as
    # long as think-time >= prove-time.
    #
    # A prepared payment reads the channel tip but never advances it (only a
    # countersignature does), so proving ahead is always safe: worst case the
    # proof is discarded unused. Each prepared payment reveals the tip's
    # committed next-nullifier N_i, so there is at most ONE per tip — re-proving
    # the same tip returns the cached one rather than burning a second nullifier.

    def _save_prepared(self, prepared: dict) -> None:
        os.makedirs(os.path.dirname(PREPARED_PATH), exist_ok=True)
        with open(PREPARED_PATH, "wb") as f:
            pickle.dump(prepared, f)
        os.chmod(PREPARED_PATH, 0o600)

    def _clear_prepared(self) -> None:
        self._prepared = None
        try:
            os.remove(PREPARED_PATH)
        except FileNotFoundError:
            pass

    def prepared_ready(self) -> dict | None:
        """A prepared payment valid for the current tip, or None. Loads the
        persisted one from a previous session so the first message stays instant
        across CLI restarts; discards it if the tip has since advanced."""
        prepared = getattr(self, "_prepared", None)
        if prepared is None and os.path.exists(PREPARED_PATH):
            try:
                with open(PREPARED_PATH, "rb") as f:
                    prepared = pickle.load(f)
            except Exception:
                prepared = None
        if prepared is None:
            return None
        payer = self._load_channel()
        if prepared.get("for_index") != payer.tip.index or prepared["m"].N_i != payer.tip.N_next:
            self._clear_prepared()
            return None
        self._prepared = prepared
        return prepared

    def channel_prove_next(self, price: int | None = None) -> dict:
        """Prove the next payment for the current tip (the ~45s step). Returns a
        prepared-payment dict to spend later with channel_pay_prepared. Safe to
        call from a background thread: it only reads the channel, and persists
        the result (not the channel) so it never races the spend path."""
        cached = self.prepared_ready()
        if cached is not None and (price is None or cached["price"] == price):
            return cached
        params = self._channel_params()
        price = price or params["price_per_request"]
        payer = self._load_channel()
        self._channel_require_prover(payer, params)
        if payer.D - payer.tip.bal < price:
            raise RuntimeError("channel balance below price; open a new channel")
        t0 = time.time()
        m, pending = payer.build_payment(price)   # proves; reads tip, no mutation
        prepared = {"for_index": payer.tip.index, "m": m, "pending": pending,
                    "price": price, "prove_s": time.time() - t0}
        self._prepared = prepared
        self._save_prepared(prepared)
        return prepared

    def channel_pay_prepared(self, prepared: dict, messages: list[dict],
                             model: str, **kwargs):
        """Spend an already-proven payment: post it with the request, take the
        countersignature, advance the tip. No proving happens here — this is the
        instant path once channel_prove_next has run ahead."""
        payer = self._load_channel()
        if prepared.get("for_index") != payer.tip.index:
            raise RuntimeError("prepared payment is stale; the tip advanced")
        m, pending, price = prepared["m"], prepared["pending"], prepared["price"]
        reply = self._channel_post(payer, m, pending, messages, model, **kwargs)
        self._save_channel(payer)
        self._clear_prepared()
        return reply, {"cost": price, "remaining": payer.D - payer.tip.bal}

    def _channel_post(self, payer: Payer, m, pending, messages: list[dict],
                      model: str, **kwargs) -> dict:
        """POST a built payment + request, apply the countersignature to `payer`
        (advancing its tip in place). Shared by the cold and pipelined paths."""
        payment_j = payment_to_j(m)
        header_b64 = base64.b64encode(json.dumps(payment_j).encode()).decode()
        body = {"model": model, "messages": messages, "stream": False, **kwargs}
        headers = {}
        # A real STARK proof (~3.8 MB base64) blows past HTTP header limits;
        # ship it in the reserved body field instead. Small (clear) proofs keep
        # using the header for backward compatibility.
        if len(header_b64) > 8000:
            body["_channel_payment"] = payment_j
        else:
            headers["X-Channel-Payment"] = header_b64
        resp = self.http.post(
            f"{self.url}/v1/chat/completions", json=body, headers=headers,
        )
        resp.raise_for_status()
        countersign = resp.headers.get("X-Channel-Countersign")
        if not countersign:
            raise RuntimeError("router did not countersign the payment")
        sigma = sig_from_j(json.loads(base64.b64decode(countersign)))
        payer.on_countersign(pending, sigma)   # advances the tip only if valid
        return resp.json()

    def channel_chat(self, messages: list[dict], model: str, **kwargs):
        """Cold single-shot channel payment: prove on the critical path, then
        spend. The interactive REPL uses channel_prove_next/pay_prepared instead
        to hide the proving latency."""
        params = self._channel_params()
        price = params["price_per_request"]
        payer = self._load_channel()
        payer_prover = self._channel_require_prover(payer, params)
        if payer.D - payer.tip.bal < price:
            raise RuntimeError("channel balance below price; open a new channel")
        if payer_prover == "sp1":
            print("proving payment (SP1 STARK, ~45s native)...", file=sys.stderr)
        t0 = time.time()
        m, pending = payer.build_payment(price)
        if payer_prover == "sp1":
            print(f"payment proof ready in {time.time() - t0:.1f}s "
                  f"({len(m.pi)} byte envelope)", file=sys.stderr)
        reply = self._channel_post(payer, m, pending, messages, model, **kwargs)
        self._save_channel(payer)
        self._clear_prepared()   # tip moved; any prepared token is now stale
        return reply, {"cost": price, "remaining": payer.D - payer.tip.bal}

    def chat(self, messages: list[dict], model: str, prepay: int = 2000,
             stream: bool = False, **kwargs):
        headers = {}
        if not model.startswith("local/"):  # local/* lane is free, no payment
            spend = [
                {"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                for t in self._select(max(prepay, self.keys()["min_prepay"]))
            ]
            headers["X-Cash"] = base64.b64encode(json.dumps(spend).encode()).decode()
        body = {"model": model, "messages": messages, "stream": stream, **kwargs}
        if stream:
            return self.http.stream(
                "POST", f"{self.url}/v1/chat/completions", json=body, headers=headers
            )
        resp = self.http.post(
            f"{self.url}/v1/chat/completions", json=body, headers=headers
        )
        receipt = resp.headers.get("X-Change-Receipt")
        settle = self.redeem_change(receipt) if receipt else {"cost": 0, "change": 0}
        resp.raise_for_status()
        return resp.json(), settle
