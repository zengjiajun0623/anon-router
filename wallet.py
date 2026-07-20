"""Client wallet: holds blind-signed tokens, pays per request, redeems change."""
from __future__ import annotations  # PEP 563: keep `X | None` hints lazy so the
# client runs on Python 3.9 (older laptops), not just 3.10+.
import base64
import json
import os
import pickle
import re
import secrets
import sys
import time

import httpx


def _needed_credits(text: str) -> int | None:
    """Parse the router's cost-bound 402 (`prepay N < M credits needed ...`) to
    learn how much this request actually needs, so the client can retry with an
    adequate prepay (big requests — e.g. Claude Code's system prompt + many tool
    schemas — need far more than the default 2000)."""
    m = re.search(r"<\s*(\d+)\s*credits needed", text)
    return int(m.group(1)) if m else None

from confetti.channel import Payer
from confetti.sp1 import RealSP1Prover
from confetti.wire import payment_to_j, sig_from_j
from mint import DENOMS, blind, decompose, unblind

# Default to the hosted service so the CLI works out of the box; override with
# ANON_ROUTER_URL (or --url) to point at a local/self-hosted router.
DEFAULT_MINT = os.environ.get(
    "ANON_ROUTER_URL", "https://anon-router-production.up.railway.app")
# Fixed voucher face values ($1/$5/$10/$20 at 1 credit = $0.0001). Redeeming
# tries these so the client never has to ask the router the voucher's value
# (that status endpoint was a probing oracle). Keep in sync with admin.py.
VOUCHER_FACE_VALUES = (10000, 50000, 100000, 200000)
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
        # No keep-alive: every request opens a fresh connection so the router
        # can't link a wallet's requests to each other by TCP/TLS session
        # (blind signatures are pointless if the transport is the identifier).
        self.http = httpx.Client(
            timeout=300, proxy=proxy,
            limits=httpx.Limits(max_keepalive_connections=0),
            headers={"Connection": "close"})
        self._load()
        self._keys = None

    def _load(self) -> None:
        if os.path.exists(self.path):
            data = json.load(open(self.path))
            self.tokens = data.get("tokens", [])
            self.account = data.get("account")   # {"api_key", "key_hash"} or None
            # An in-flight spend whose response was lost: {tokens, blanks}. Held
            # until the next request recovers its change (or restores the tokens).
            self.pending = data.get("pending")
        else:
            self.tokens = []
            self.account = None
            self.pending = None

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"mint": self.url, "tokens": self.tokens,
                       "account": self.account, "pending": self.pending}, f, indent=1)
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

    def _blind_outputs(self, amounts: list[int]) -> list[dict]:
        """Blind a fresh secret for each denomination; returns the client-side
        records {amount, secret, r, B_} to send and later unblind."""
        blinds = []
        for denom in amounts:
            secret = secrets.token_hex(32)
            blinded_hex, r = blind(secret)
            blinds.append({"amount": denom, "secret": secret, "r": r, "B_": blinded_hex})
        return blinds

    def _store_signatures(self, blinds: list[dict], sigs: list[dict]) -> list[dict]:
        """Unblind mint signatures into spendable tokens and store them. `sigs`
        may be a PREFIX of `blinds` (in-band change signs only decompose(change));
        each sig carries its assigned amount, matched to blinds by position."""
        pubkeys = self.keys()["pubkeys"]
        minted = []
        for b, sig in zip(blinds, sigs):
            amount = int(sig.get("amount", b["amount"]))
            c_hex = unblind(sig["C_"], b["r"], pubkeys[str(amount)])
            minted.append({"amount": amount, "secret": b["secret"], "C": c_hex})
        self.tokens.extend(minted)
        self._save()
        return minted

    def _request_signatures(self, endpoint: str, amount: int,
                            headers: dict | None = None) -> list[dict]:
        """Blind fresh secrets for `amount`, post to endpoint, unblind, store."""
        blinds = self._blind_outputs(decompose(amount))
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
        return self._store_signatures(blinds, resp.json()["signatures"])

    # ---- in-band change (Cashu NUT-08 style) ----

    def _make_change_blanks(self) -> list[dict]:
        """A fixed number (one per denomination) of blinded blank outputs sent
        WITH a spend. Fixed count => the header size never encodes the change."""
        return self._blind_outputs(list(DENOMS))

    def _change_header(self, blanks: list[dict]) -> str:
        return base64.b64encode(
            json.dumps([{"B_": b["B_"]} for b in blanks]).encode()).decode()

    def _absorb_change(self, payload: dict, blanks: list[dict]) -> dict:
        """Unblind the in-band change signatures into tokens. `payload` is the
        {change, cost, signatures} object from the response header or SSE event."""
        sigs = payload.get("signatures") or []
        if sigs:
            self._store_signatures(blanks, sigs)
        return {"cost": payload.get("cost"), "change": payload.get("change", 0)}

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

    def claim_all(self) -> int:
        """Balance-less funding: drain the ENTIRE account balance into ecash in
        one claim, so the account holds no value between sessions and there is no
        recurring, re-linkable claim event tied to spending. Call this right
        after funding; the account then serves only as a funding rendezvous."""
        if not self.account:
            raise RuntimeError("no account; run: cli.py account")
        bal = self.account_status().get("balance", 0)
        if bal > 0:
            self.claim_from_account(self.account["api_key"], bal)
        return self.balance()

    def redeem_voucher(self, code: str) -> int:
        """Redeem a voucher into ecash. The code goes in the request BODY (never
        the URL) and there is no status pre-check (that endpoint was an oracle);
        an invalid/spent code just raises."""
        # The mint signs outputs summing to the voucher's face value. We don't
        # know the value without asking, so try each fixed face value. Reuse the
        # SAME blinds across a transport retry so that if the first POST redeemed
        # the voucher but the response was lost, the retry recovers the cached
        # signatures (the server matches them by the identical blinds) instead of
        # losing the voucher's value.
        for credits in VOUCHER_FACE_VALUES:
            blinds = self._blind_outputs(decompose(credits))
            body = {"code": code, "outputs": [{"amount": b["amount"], "B_": b["B_"]}
                                              for b in blinds]}
            for attempt in range(3):
                try:
                    resp = self.http.post(f"{self.url}/mint/redeem", json=body)
                    break
                except httpx.TransportError:
                    if attempt == 2:
                        raise
            if resp.status_code == 200:
                self._store_signatures(blinds, resp.json()["signatures"])
                return self.balance()
            if resp.status_code != 400:
                resp.raise_for_status()
        raise RuntimeError("voucher invalid, already redeemed, or non-standard value")

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

    def _recover_pending(self) -> None:
        """Settle an interrupted spend before the next request. Re-presents the
        same tokens with X-Cash-Recover: if the router already spent them, absorb
        the change in-band; if it never did (404), restore the tokens as
        spendable. Never re-runs inference, never double-spends."""
        p = self.pending
        if not p:
            return
        tokens, blanks = p["tokens"], p["blanks"]
        headers = {"X-Cash": self._xcash(tokens),
                   "X-Cash-Change": self._change_header(blanks),
                   "X-Cash-Recover": "1"}
        body = {"model": "recover", "messages": [{"role": "user", "content": "."}]}
        try:
            resp = self.http.post(f"{self.url}/v1/chat/completions",
                                  json=body, headers=headers)
        except httpx.TransportError:
            # Router still unreachable. ABORT the caller (raise) rather than
            # returning — otherwise it would start a new spend and overwrite this
            # unresolved `pending`, stranding the in-flight change.
            raise RuntimeError("router unreachable; rerun to recover pending change")
        if resp.status_code == 404:      # never spent -> tokens are still good
            self.tokens.extend(tokens)
            self.pending = None
            self._save()
        elif resp.status_code == 200:    # spent -> absorb the change, then clear
            self._absorb_change(resp.json(), blanks)
            self.pending = None
            self._save()
        elif resp.status_code == 409:
            # Still in flight. Leave `pending` set and ABORT the caller so it does
            # NOT start a new spend that would overwrite this unresolved record
            # (losing the in-flight change).
            raise RuntimeError("previous request still settling; rerun shortly to recover")
        else:
            resp.raise_for_status()

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

    @staticmethod
    def _xcash(chosen: list[dict]) -> str:
        spend = [{"amount": t["amount"], "secret": t["secret"], "C": t["C"]}
                 for t in chosen]
        return base64.b64encode(json.dumps(spend).encode()).decode()

    def open_stream(self, body: dict, prepay: int = 2000):
        """Attach ecash + blinded change blanks and open a STREAMING POST,
        self-healing around dead tokens. Returns an httpx streaming Response the
        caller iterates with .iter_lines(); the caller must capture the trailing
        `event: x-cash-change` SSE event and pass its data to finish_stream()."""
        self._recover_pending()
        need = max(prepay, self.keys()["min_prepay"])
        for _ in range(min(len(self.tokens) + 4, 40)):
            chosen = self._select(need)  # raises if empty -> caller surfaces
            blanks = self._make_change_blanks()
            self.pending = {"tokens": chosen, "blanks": blanks}
            self._save()
            req = self.http.build_request(
                "POST", f"{self.url}/v1/chat/completions", json=body,
                headers={"X-Cash": self._xcash(chosen),
                         "X-Cash-Change": self._change_header(blanks)})
            resp = self.http.send(req, stream=True)
            if resp.status_code == 400 and "invalid token" in resp.read().decode(
                    "utf-8", "ignore").lower():
                resp.close()
                self.pending = None  # dead tokens weren't spent; drop them
                self._save()
                continue  # dropped by _select; retry with the rest
            if resp.status_code == 402:
                # Cost-bound rejection (PRE-spend): the request needs more prepay
                # than we attached. Restore the tokens and RETRY with enough — big
                # requests (Claude Code) far exceed the default prepay.
                text = resp.read().decode("utf-8", "ignore")
                resp.close()
                self.tokens.extend(chosen)
                self.pending = None
                self._save()
                needed = _needed_credits(text)
                if needed and needed > need and needed <= self.balance():
                    need = needed
                    continue
                raise RuntimeError(
                    f"insufficient ecash for this request (needs {needed or '?'}, "
                    f"have {self.balance()}); run: cli.py claim <credits>")
            if resp.status_code == 400:
                self.tokens.extend(chosen)  # pre-spend validation error
                self.pending = None
                self._save()
                resp.close()
                resp.raise_for_status()
            if resp.status_code >= 300:
                # 409/5xx/other: the spend MAY have committed. Keep `pending`, do
                # NOT restore the tokens; recover on the next call.
                resp.close()
                raise RuntimeError(
                    f"request failed ({resp.status_code}); rerun to recover change")
            return resp
        raise RuntimeError("no spendable ecash token; run: cli.py claim")

    def finish_stream(self, change_payload: dict | None) -> dict:
        """Settle a streamed spend with the parsed x-cash-change SSE event. If no
        change event arrived (mid-stream disconnect), leaves `pending` set so the
        next request recovers the change via X-Cash-Recover."""
        if change_payload is not None:
            blanks = (self.pending or {}).get("blanks", [])
            settle = self._absorb_change(change_payload, blanks)
            self.pending = None
            self._save()
            return settle
        return {"cost": None, "change": 0}

    def chat(self, messages: list[dict], model: str, prepay: int = 2000,
             stream: bool = False, **kwargs):
        self._recover_pending()  # settle any interrupted prior spend first
        url = f"{self.url}/v1/chat/completions"
        body = {"model": model, "messages": messages, "stream": stream, **kwargs}

        if model.startswith("local/"):  # free lane, no payment
            if stream:
                return self.http.stream("POST", url, json=body)
            resp = self.http.post(url, json=body)
            resp.raise_for_status()
            return resp.json(), {"cost": 0, "change": 0}

        if stream:
            raise RuntimeError("use open_stream() for streaming ecash requests")

        need = max(prepay, self.keys()["min_prepay"])
        # Self-healing ecash: a token the router rejects as an INVALID SIGNATURE
        # was signed by a mint epoch this router no longer honors. _select has
        # already removed the attempted tokens, so we retry with the rest — a few
        # dead tokens can't brick the wallet. Change comes back IN-BAND (in the
        # X-Cash-Change response header), so there is no separate redeem call.
        for _ in range(len(self.tokens) + 4):
            try:
                chosen = self._select(need)
            except RuntimeError:
                raise RuntimeError(
                    "all ecash tokens were rejected as invalid (signed by a router "
                    "that rotated its mint key). Run: cli.py claim <credits>")
            blanks = self._make_change_blanks()
            self.pending = {"tokens": chosen, "blanks": blanks}  # crash-recovery
            self._save()
            headers = {"X-Cash": self._xcash(chosen),
                       "X-Cash-Change": self._change_header(blanks)}
            try:
                resp = self.http.post(url, json=body, headers=headers)
            except httpx.TransportError:
                # Response may be lost after the spend; keep `pending` so the next
                # call recovers the change instead of losing it.
                raise RuntimeError("request interrupted; rerun to recover change")
            # If the router returned change, the tokens WERE spent — absorb it
            # regardless of HTTP status (success OR an upstream-error full refund),
            # then surface any error. Never restore tokens once change came back.
            hdr = resp.headers.get("X-Cash-Change")
            if hdr:
                settle = self._absorb_change(json.loads(base64.b64decode(hdr)), blanks)
                self.pending = None
                self._save()
                if resp.status_code >= 400:
                    resp.raise_for_status()
                return resp.json(), settle
            if resp.status_code == 400 and "invalid token" in resp.text.lower():
                self.pending = None  # dead tokens weren't spent; drop them
                self._save()
                continue
            if resp.status_code == 402:
                # Cost-bound rejection (PRE-spend): restore tokens and RETRY with
                # enough prepay (a big request needs more than the default).
                self.tokens.extend(chosen)
                self.pending = None
                self._save()
                needed = _needed_credits(resp.text)
                if needed and needed > need and needed <= self.balance():
                    need = needed
                    continue
                raise RuntimeError(
                    f"insufficient ecash for this request (needs {needed or '?'}, "
                    f"have {self.balance()}); run: cli.py claim <credits>")
            if resp.status_code == 400:
                # PRE-spend validation error; tokens not burned, restore them.
                self.tokens.extend(chosen)
                self.pending = None
                self._save()
                resp.raise_for_status()
            if resp.status_code >= 300:
                # 409 or 5xx or anything else: the spend MAY have committed but no
                # change came back. Do NOT restore the tokens (that would strand a
                # real spend). Keep `pending`; the next call recovers via
                # X-Cash-Recover (404 -> restore, receipt -> absorb change).
                raise RuntimeError(
                    f"request failed ({resp.status_code}); rerun to recover change")
            self.pending = None  # 2xx with no change header (no change owed)
            self._save()
            return resp.json(), {"cost": None, "change": 0}
        raise RuntimeError("could not find a spendable ecash token; run: cli.py claim")
