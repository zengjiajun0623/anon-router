"""Client wallet: holds blind-signed tokens, pays per request, redeems change."""
import base64
import json
import os
import secrets
import time

import httpx

from mint import blind, decompose, unblind

DEFAULT_MINT = os.environ.get("ANON_ROUTER_URL", "http://127.0.0.1:8402")
WALLET_PATH = os.path.expanduser("~/.anon-router/wallet.json")


class Wallet:
    def __init__(self, mint_url: str = DEFAULT_MINT, path: str = WALLET_PATH):
        self.url = mint_url.rstrip("/")
        self.path = path
        self.http = httpx.Client(timeout=300)
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

    def _request_signatures(self, endpoint: str, amount: int) -> list[dict]:
        """Blind fresh secrets for `amount`, post to endpoint, unblind, store."""
        blinds = []
        for denom in decompose(amount):
            secret = secrets.token_hex(32)
            blinded_hex, r = blind(secret)
            blinds.append({"amount": denom, "secret": secret, "r": r, "B_": blinded_hex})
        resp = self.http.post(
            f"{self.url}{endpoint}",
            json={"outputs": [{"amount": b["amount"], "B_": b["B_"]} for b in blinds]},
        )
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
