"""JSON (de)serialization of channel objects for transport over HTTP."""
from __future__ import annotations

from .channel import PaymentMessage
from .relation import _sig_from_j, _sig_to_j  # reuse the XMSS codec
from .wots import XmssSignature


def payment_to_j(m: PaymentMessage) -> dict:
    return {"N_i": m.N_i.hex(), "delta": m.delta, "C_i": m.C_i.hex(),
            "pi": m.pi.decode(), "root": m.root.hex()}


def payment_from_j(d: dict) -> PaymentMessage:
    return PaymentMessage(bytes.fromhex(d["N_i"]), d["delta"],
                          bytes.fromhex(d["C_i"]), d["pi"].encode(),
                          bytes.fromhex(d["root"]))


def sig_to_j(s: XmssSignature) -> dict:
    return _sig_to_j(s)


def sig_from_j(d: dict) -> XmssSignature:
    return _sig_from_j(d)
