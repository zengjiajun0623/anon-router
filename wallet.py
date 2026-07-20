"""Client wallet: holds blind-signed tokens, pays per request, redeems change."""
import base64
import json
import os
import pickle
import secrets
import time

import httpx

from confetti.channel import Payer
from confetti.wire import payment_to_j, sig_from_j
from mint import blind, decompose, unblind

DEFAULT_MINT = os.environ.get("ANON_ROUTER_URL", "http://127.0.0.1:8402")
WALLET_PATH = os.path.expanduser("~/.anon-router/wallet.json")
CHANNEL_PATH = os.path.expanduser("~/.anon-router/channel.pkl")


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
            self.tokens = json.load(open(self.path)).get("tokens", [])
        else:
            self.tokens = []

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"mint": self.url, "tokens": self.tokens}, f, indent=1)
        os.chmod(self.path, 0o600)

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
        Persists the Payer locally (demo persistence; see channel.py notes)."""
        params = self.http.get(f"{self.url}/channel/params").json()
        payer = Payer(deposit, bytes.fromhex(params["pk_B"]))
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

    def channel_chat(self, messages: list[dict], model: str, **kwargs):
        params = self.http.get(f"{self.url}/channel/params").json()
        price = params["price_per_request"]
        payer = self._load_channel()
        if payer.D - payer.tip.bal < price:
            raise RuntimeError("channel balance below price; open a new channel")
        m, pending = payer.build_payment(price)
        header = base64.b64encode(json.dumps(payment_to_j(m)).encode()).decode()
        body = {"model": model, "messages": messages, "stream": False, **kwargs}
        resp = self.http.post(
            f"{self.url}/v1/chat/completions", json=body,
            headers={"X-Channel-Payment": header},
        )
        resp.raise_for_status()
        countersign = resp.headers.get("X-Channel-Countersign")
        if not countersign:
            raise RuntimeError("router did not countersign the payment")
        sigma = sig_from_j(json.loads(base64.b64decode(countersign)))
        payer.on_countersign(pending, sigma)   # advances the tip only if valid
        self._save_channel(payer)
        return resp.json(), {"cost": price, "remaining": payer.D - payer.tip.bal}

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
