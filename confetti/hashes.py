"""Hash primitives for the confetti channel: H (random-oracle-modeled) and
Com (hiding, binding commitment). Both are domain-separated sha256 in this
reference implementation; the spec models them as Poseidon/Blake class, which
a STARK backend would swap in without changing any protocol logic.
"""
import hashlib
from typing import Iterable

N_BYTES = 32  # nullifier / commitment / digest width


def _h(domain: bytes, *parts: bytes) -> bytes:
    m = hashlib.sha256()
    m.update(domain)
    for p in parts:
        m.update(len(p).to_bytes(4, "big"))
        m.update(p)
    return m.digest()


def H(*parts: bytes) -> bytes:
    """The chain/random-oracle hash used for nullifiers and the WOTS chains."""
    return _h(b"confetti/H", *parts)


def commit(*parts: bytes, r: bytes) -> bytes:
    """Hiding, binding commitment. `r` is the blinding randomness."""
    return _h(b"confetti/Com", r, *parts)


def i2b(x: int, width: int = N_BYTES) -> bytes:
    return int(x).to_bytes(width, "big")


def concat(chunks: Iterable[bytes]) -> bytes:
    return b"".join(chunks)
