"""R_pay — the payment relation (Spec-v2 §3) and the prover interface.

The relation is the trust core: it constrains a payment without revealing the
channel, balances, or which prior state it extends. This module defines

  * `check_R_pay` — the constraint system, as plain Python. This is exactly
    what a STARK circuit must encode; the reference verifier and the eventual
    zkVM guest share these checks.
  * `Prover` — the swappable proving interface. `ClearWitnessProver` is the
    reference: `pi` carries the witness in the clear and the verifier re-runs
    `check_R_pay`. It provides knowledge-soundness (every constraint is
    checked) but NOT zero-knowledge — the one property gated on the STARK
    backend. Swapping in `Sp1Prover` later changes nothing else in the stack.

Public inputs:  (delta, N_i, C_i, root)
Private witness: cid, D, c, r_open, bal_prev, r_i, and a parent branch that is
either Genesis or SignedParent (the disjunction R_pay hides).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from .chain import null_first, null_next, open_commit, state_commit
from .hashes import H
from .merkle import verify_membership
from .wots import XmssSignature, xmss_verify


@dataclass
class Statement:
    delta: int
    N_i: bytes
    C_i: bytes
    root: bytes


@dataclass
class GenesisBranch:
    """Parent is the on-chain genesis: bal_prev = 0, N_i = H(cid, c)."""
    rec_index: int
    rec_path: list  # Merkle path of the channel record to `root`


@dataclass
class SignedBranch:
    """Parent is a Bob-signed state whose committed next-nullifier is N_i."""
    C_prev: bytes
    r_prev: bytes
    sigma_prev: XmssSignature
    rec_index: int
    rec_path: list  # channel-record membership for pk_B (GN-1)


@dataclass
class Witness:
    cid: bytes
    D: int
    c: bytes
    r_open: bytes
    bal_prev: int
    bal_i: int
    r_i: bytes
    pk_B: bytes
    # channel-record fields needed to reconstruct the leaf for membership
    C_open: bytes
    branch: object  # GenesisBranch | SignedBranch


def _record_leaf(cid: bytes, D: int, pk_B: bytes, C_open: bytes) -> bytes:
    from .hashes import i2b
    return H(b"chrec", cid, i2b(D), pk_B, C_open)


def check_R_pay(st: Statement, w: Witness) -> Optional[str]:
    """Return None if the witness satisfies R_pay for the statement, else the
    first violated constraint (as a string, for test diagnostics)."""
    leaf = _record_leaf(w.cid, w.D, w.pk_B, w.C_open)

    # Chain-secret binding (Spec §3 constraint 2): the `c` used in the chain
    # equation must be the one committed at open. Guards BOTH branches — the
    # signed branch previously left `c` a free witness (review BUG 3).
    if open_commit(w.c, w.r_open) != w.C_open:
        return "C_open does not open to committed c"

    # 1. Parent branch (flat disjunction, hidden in ZK).
    if isinstance(w.branch, GenesisBranch):
        b = w.branch
        if not verify_membership(st.root, leaf, b.rec_index, b.rec_path):
            return "genesis: channel record not in root"
        if w.bal_prev != 0:
            return "genesis: bal_prev != 0"
        if null_first(w.cid, w.c) != st.N_i:
            return "genesis: N_i != H(cid, c)"
    elif isinstance(w.branch, SignedBranch):
        b = w.branch
        if not verify_membership(st.root, leaf, b.rec_index, b.rec_path):
            return "signed: channel record (pk_B) not in root"
        # The parent's committed next-nullifier IS the revealed N_i.
        if state_commit(w.cid, w.D, w.bal_prev, st.N_i, b.r_prev) != b.C_prev:
            return "signed: C_prev does not commit (cid,D,bal_prev,N_i)"
        if not xmss_verify(w.pk_B, b.C_prev, b.sigma_prev):
            return "signed: Bob signature on C_prev invalid"
    else:
        return "no valid parent branch"

    # 2. Chain equation: N_{i+1} = H(N_i, c), and it is what C_i commits to.
    n_next = null_next(st.N_i, w.c)

    # 3. Value update.
    if st.delta < 0:
        return "delta < 0"
    if w.bal_i != w.bal_prev + st.delta:
        return "bal_i != bal_prev + delta"
    if w.bal_i > w.D:
        return "bal_i > D (overspend)"

    # 4. Output binding.
    if state_commit(w.cid, w.D, w.bal_i, n_next, w.r_i) != st.C_i:
        return "C_i does not bind (cid,D,bal_i,N_{i+1})"
    return None


class Prover(Protocol):
    def prove(self, st: Statement, w: Witness) -> bytes: ...
    def verify(self, st: Statement, pi: bytes) -> bool: ...


# Explicit (de)serialization: dataclasses.asdict would recursively flatten the
# nested XmssSignature into an untagged dict, so we serialize by hand and tag
# every non-JSON type. bytes -> hex string; XmssSignature -> tagged object.

def _sig_to_j(s: XmssSignature) -> dict:
    return {"index": s.index,
            "wots_sig": [b.hex() for b in s.wots_sig],
            "auth_path": [b.hex() for b in s.auth_path]}


def _sig_from_j(d: dict) -> XmssSignature:
    return XmssSignature(d["index"],
                         [bytes.fromhex(x) for x in d["wots_sig"]],
                         [bytes.fromhex(x) for x in d["auth_path"]])


def _branch_to_j(b) -> dict:
    if isinstance(b, GenesisBranch):
        return {"rec_index": b.rec_index, "rec_path": [p.hex() for p in b.rec_path]}
    return {"C_prev": b.C_prev.hex(), "r_prev": b.r_prev.hex(),
            "sigma_prev": _sig_to_j(b.sigma_prev),
            "rec_index": b.rec_index, "rec_path": [p.hex() for p in b.rec_path]}


def _branch_from_j(kind: str, d: dict):
    path = [bytes.fromhex(p) for p in d["rec_path"]]
    if kind == "GenesisBranch":
        return GenesisBranch(d["rec_index"], path)
    return SignedBranch(bytes.fromhex(d["C_prev"]), bytes.fromhex(d["r_prev"]),
                        _sig_from_j(d["sigma_prev"]), d["rec_index"], path)


def _witness_to_j(w: Witness) -> dict:
    return {"cid": w.cid.hex(), "D": w.D, "c": w.c.hex(), "r_open": w.r_open.hex(),
            "bal_prev": w.bal_prev, "bal_i": w.bal_i, "r_i": w.r_i.hex(),
            "pk_B": w.pk_B.hex(), "C_open": w.C_open.hex(),
            "branch_kind": type(w.branch).__name__, "branch": _branch_to_j(w.branch)}


def _witness_from_j(d: dict) -> Witness:
    return Witness(cid=bytes.fromhex(d["cid"]), D=d["D"], c=bytes.fromhex(d["c"]),
                   r_open=bytes.fromhex(d["r_open"]), bal_prev=d["bal_prev"],
                   bal_i=d["bal_i"], r_i=bytes.fromhex(d["r_i"]),
                   pk_B=bytes.fromhex(d["pk_B"]), C_open=bytes.fromhex(d["C_open"]),
                   branch=_branch_from_j(d["branch_kind"], d["branch"]))


class ClearWitnessProver:
    """Reference prover: pi = the witness in the clear. Sound, not ZK.

    A real backend (SP1/RISC0) replaces this with a STARK whose public inputs
    are `st` and whose verify() checks the proof — check_R_pay stays the
    circuit spec. Nothing else in the protocol depends on which prover is used.
    """

    zero_knowledge = False

    def prove(self, st: Statement, w: Witness) -> bytes:
        err = check_R_pay(st, w)
        if err:
            raise ValueError(f"cannot prove invalid statement: {err}")
        # Dev-only: simulate the real STARK prover's ~45s latency so the
        # pipelining path can be exercised without the 28 GB SP1 proof. Never
        # set in production (the clear prover is not zero-knowledge anyway).
        fake = os.environ.get("ANON_FAKE_PROVE_S")
        if fake:
            time.sleep(float(fake))
        return json.dumps(_witness_to_j(w)).encode()

    def verify(self, st: Statement, pi: bytes) -> bool:
        try:
            w = _witness_from_j(json.loads(pi.decode()))
        except Exception:
            return False
        return check_R_pay(st, w) is None

    def close_values(self, st: Statement, pi: bytes) -> dict:
        """The proof-bound (bal, N_next) an unsigned close settles on.

        These are what a real R_closeUnsigned circuit would expose as public
        inputs, constrained to C_x. Deriving them from the verified proof (not
        from caller arguments) is the fix for the unsigned-close theft (review
        BUG 1): both are bound to C_i via the output-binding constraint, so a
        caller cannot claim a lower balance or a bogus exhibit set.
        """
        w = _witness_from_j(json.loads(pi.decode()))
        return {"bal": w.bal_i, "n_next": null_next(st.N_i, w.c)}
