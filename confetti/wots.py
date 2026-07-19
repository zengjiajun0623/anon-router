"""Post-quantum signatures for Bob (the recipient/router).

Bob countersigns many state commitments over a channel's life, so the
one-time WOTS primitive is wrapped in a small Merkle tree (XMSS-lite) giving
`2^height` one-time keys under a single public root. Verification is pure
hashing, which is the property that makes it cheap inside a STARK (the R_pay
signed branch verifies one WOTS signature in-circuit).

Reference implementation: plain Winternitz (w=16, len=67 over sha256),
matching the Phase-0 benchmark circuit. WOTS+ bitmasks are omitted; they
tighten the security reduction but change no protocol logic. Do not reuse a
leaf index — the XMSS wrapper enforces strict monotonic index use.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .hashes import H

W = 16              # Winternitz parameter
LOG_W = 4           # log2(W)
N = 32              # hash width in bytes
LEN1 = 64           # ceil(256 / LOG_W)
LEN2 = 3            # floor(log2(LEN1*(W-1))/LOG_W) + 1
LEN = LEN1 + LEN2   # 67 chains


def _chain(x: bytes, start: int, steps: int, idx: int) -> bytes:
    """Apply the hash chain `steps` times starting at height `start`."""
    out = x
    for h in range(start, start + steps):
        out = H(b"wots-chain", idx.to_bytes(2, "big"), h.to_bytes(2, "big"), out)
    return out


def _digits(msg32: bytes) -> list[int]:
    """Base-w digits of the message plus the Winternitz checksum."""
    ds = []
    for byte in msg32:
        ds.append(byte >> LOG_W)
        ds.append(byte & 0x0F)
    checksum = sum(W - 1 - d for d in ds)
    for shift in (8, 4, 0):
        ds.append((checksum >> shift) & 0x0F)
    return ds[:LEN]


@dataclass
class WotsKey:
    sk: list[bytes]
    pk: list[bytes]

    @staticmethod
    def generate() -> "WotsKey":
        sk = [secrets.token_bytes(N) for _ in range(LEN)]
        pk = [_chain(sk[i], 0, W - 1, i) for i in range(LEN)]
        return WotsKey(sk, pk)

    def leaf(self) -> bytes:
        return H(b"wots-leaf", *self.pk)

    def sign(self, msg32: bytes) -> list[bytes]:
        ds = _digits(msg32)
        return [_chain(self.sk[i], 0, ds[i], i) for i in range(LEN)]


def wots_pk_from_sig(msg32: bytes, sig: list[bytes]) -> list[bytes]:
    ds = _digits(msg32)
    return [_chain(sig[i], ds[i], W - 1 - ds[i], i) for i in range(LEN)]


def wots_leaf_from_sig(msg32: bytes, sig: list[bytes]) -> bytes:
    return H(b"wots-leaf", *wots_pk_from_sig(msg32, sig))


# ---- XMSS-lite: Merkle tree over 2^height WOTS leaves ----

def _merkle_parent(left: bytes, right: bytes) -> bytes:
    return H(b"xmss-node", left, right)


@dataclass
class XmssSignature:
    index: int
    wots_sig: list[bytes]
    auth_path: list[bytes]


class Xmss:
    """Many-time hash-based signature. `pk` is the Merkle root."""

    def __init__(self, height: int = 10):
        self.height = height
        self.n_leaves = 1 << height
        self._keys = [WotsKey.generate() for _ in range(self.n_leaves)]
        self._leaves = [k.leaf() for k in self._keys]
        self._layers = self._build(self._leaves)
        self.pk = self._layers[-1][0]
        self._next = 0

    @staticmethod
    def _build(leaves: list[bytes]) -> list[list[bytes]]:
        layers = [leaves]
        while len(layers[-1]) > 1:
            cur = layers[-1]
            layers.append(
                [_merkle_parent(cur[i], cur[i + 1]) for i in range(0, len(cur), 2)]
            )
        return layers

    def _auth_path(self, index: int) -> list[bytes]:
        path, idx = [], index
        for layer in self._layers[:-1]:
            sib = idx ^ 1
            path.append(layer[sib])
            idx >>= 1
        return path

    def sign(self, msg32: bytes) -> XmssSignature:
        if self._next >= self.n_leaves:
            raise RuntimeError("XMSS key exhausted")
        idx = self._next
        self._next += 1
        return XmssSignature(idx, self._keys[idx].sign(msg32), self._auth_path(idx))


def xmss_verify(root: bytes, msg32: bytes, sig: XmssSignature) -> bool:
    node = wots_leaf_from_sig(msg32, sig.wots_sig)
    idx = sig.index
    for sib in sig.auth_path:
        node = _merkle_parent(node, sib) if idx & 1 == 0 else _merkle_parent(sib, node)
        idx >>= 1
    return node == root
