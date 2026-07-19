"""Nullifier chain, state commitments, and channel records (Spec-v2 §2)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from .hashes import H, commit, i2b


def null_first(cid: bytes, c: bytes) -> bytes:
    """N_1 = H(cid, c)."""
    return H(b"null", cid, c)


def null_next(n_prev: bytes, c: bytes) -> bytes:
    """N_{j+1} = H(N_j, c)."""
    return H(b"null", n_prev, c)


def state_commit(cid: bytes, D: int, bal: int, n_next: bytes, r: bytes) -> bytes:
    """C_i = Com(cid, D, bal_i, N_{i+1} ; r_i)  (Spec-v2 §2, A1+A4)."""
    return commit(cid, i2b(D), i2b(bal), n_next, r=r)


def open_commit(c: bytes, r_open: bytes) -> bytes:
    """C_open = Com(c ; r_open)."""
    return commit(c, r=r_open)


@dataclass(frozen=True)
class ChannelRecord:
    """On-chain channel record `ch = (cid, D, pk_B, C_open)` (Spec-v2 §2)."""
    cid: bytes
    D: int
    pk_B: bytes
    C_open: bytes

    def leaf(self) -> bytes:
        return H(b"chrec", self.cid, i2b(self.D), self.pk_B, self.C_open)


def new_cid() -> bytes:
    return secrets.token_bytes(16)


def new_secret() -> bytes:
    return secrets.token_bytes(32)


def new_rand() -> bytes:
    return secrets.token_bytes(32)
