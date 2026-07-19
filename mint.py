"""Blind-signature mint (Cashu-style BDHKE on secp256k1).

Flow:
  wallet: secret s -> Y = hash_to_curve(s); pick r; B_ = Y + r*G  -> send B_
  mint:   C_ = k_d * B_                                          -> return C_
  wallet: C = C_ - r*K_d  (K_d = k_d*G)   token = (d, s, C)
  spend:  mint checks k_d * hash_to_curve(s) == C and s unseen.

The mint signs blinded points, so issued tokens are unlinkable to spends.
One key per denomination binds the amount to the signature.
"""
import hashlib
import hmac
import secrets as _secrets

import ec

HTC_DOMAIN = b"anon-router-htc-v1"
DENOMS = [1 << i for i in range(21)]  # 1 .. 1,048,576 credits


def hash_to_curve(msg: bytes) -> ec.Point:
    counter = 0
    while counter < 2**16:
        digest = hashlib.sha256(HTC_DOMAIN + msg + counter.to_bytes(4, "little")).digest()
        try:
            return ec.decompress(b"\x02" + digest)
        except ValueError:
            counter += 1
    raise ValueError("hash_to_curve: no valid point found")


def decompose(amount: int) -> list[int]:
    """Split an amount into power-of-two denominations, largest first."""
    out = []
    for d in reversed(DENOMS):
        while amount >= d:
            out.append(d)
            amount -= d
    return out


class Mint:
    def __init__(self, master: bytes):
        self.keys = {}
        for d in DENOMS:
            k = int.from_bytes(hmac.new(master, str(d).encode(), hashlib.sha256).digest(), "big") % ec.N
            self.keys[d] = k or 1

    def pubkeys(self) -> dict[int, str]:
        return {d: ec.compress(ec.mul(k, ec.G)).hex() for d, k in self.keys.items()}

    def sign_blinded(self, denom: int, blinded_hex: str) -> str:
        if denom not in self.keys:
            raise ValueError(f"unknown denomination {denom}")
        blinded = ec.decompress(bytes.fromhex(blinded_hex))
        return ec.compress(ec.mul(self.keys[denom], blinded)).hex()

    def verify(self, denom: int, secret: str, c_hex: str) -> bool:
        if denom not in self.keys:
            return False
        try:
            claimed = ec.decompress(bytes.fromhex(c_hex))
        except ValueError:
            return False
        return ec.mul(self.keys[denom], hash_to_curve(secret.encode())) == claimed


def blind(secret: str) -> tuple[str, int]:
    y = hash_to_curve(secret.encode())
    r = int.from_bytes(_secrets.token_bytes(32), "big") % ec.N or 1
    blinded = ec.add(y, ec.mul(r, ec.G))
    return ec.compress(blinded).hex(), r


def unblind(signed_hex: str, r: int, mint_pub_hex: str) -> str:
    signed = ec.decompress(bytes.fromhex(signed_hex))
    r_times_k = ec.mul(r, ec.decompress(bytes.fromhex(mint_pub_hex)))
    return ec.compress(ec.add(signed, ec.neg(r_times_k))).hex()
