"""RealSP1Prover — the SP1 STARK backend for R_pay (Phase 4: full disjunction).

Drop-in implementation of the swappable `Prover` protocol from relation.py.
Unlike ClearWitnessProver, the payment proof `pi` reveals NOTHING beyond the
public statement (delta, N_i, C_i, root): the witness (cid, c, r_open,
balances, r_i, Merkle position, the parent branch and Bob's signature) stays
on the client.

Mechanics
  * prove()  — shells out to the `rpay` host binary (research/m4b-groth16),
    which embeds the R_pay guest ELF (genesis OR signed parent, the flat
    disjunction of check_R_pay) and produces a core SP1 STARK (native CPU on
    an Apple-silicon laptop, ~3 MB). Returns a self-describing JSON envelope:
    {"scheme": ..., "proof": base64(bincode)}.
  * verify() — `rpay verify` checks the STARK against the vkey of the
    embedded guest AND that the proof's committed public values equal
    abi.encode(delta, N_i, C_i, root); statement binding is enforced in the
    binary, not here.

Branch hiding: the public inputs are identical for both branches, and the
guest always executes BOTH branch checks — a genesis payment feeds uniformly
random dummies into the signed-branch slots (same distribution as real parent
commitments/signature chains), so neither the statement, the proof shape, nor
the cycle-count distribution reveals whether a payment is the channel's first
(genesis) or a later one (signed parent). The dummy XMSS auth path uses
`xmss_height` (default 12 — the router's CHANNEL_HEIGHT default) so its length
matches real countersignatures.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import tempfile
from typing import Optional

from .relation import GenesisBranch, SignedBranch, Statement, Witness, check_R_pay

SCHEME = "sp1-rpay-core-v1"

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BIN = os.path.join(_REPO, "research", "m4b-groth16", "target", "release", "rpay")

PROVE_TIMEOUT_S = 900
VERIFY_TIMEOUT_S = 300

WOTS_LEN = 67                # confetti/wots.py LEN (w=16 over sha256)
DEFAULT_XMSS_HEIGHT = 12     # router Recipient default (server CHANNEL_HEIGHT)


def _signed_fields(w: Witness, xmss_height: int) -> dict:
    """The signed-branch witness slots — real parent data for a SignedBranch,
    fresh uniform randomness for genesis (identical distribution: commitments
    and hash-chain values are uniform 32-byte strings, so the guest's chain
    walk over the dummies is statistically indistinguishable from a real
    verify — the branch does not leak through the proof)."""
    b = w.branch
    if isinstance(b, SignedBranch):
        s = b.sigma_prev
        return {
            "C_prev": b.C_prev.hex(),
            "r_prev": b.r_prev.hex(),
            "sig_index": s.index,
            "wots_sig": b"".join(s.wots_sig).hex(),
            "auth_path": b"".join(s.auth_path).hex(),
        }
    return {
        "C_prev": secrets.token_bytes(32).hex(),
        "r_prev": secrets.token_bytes(32).hex(),
        "sig_index": secrets.randbelow(1 << xmss_height),
        "wots_sig": secrets.token_bytes(WOTS_LEN * 32).hex(),
        "auth_path": secrets.token_bytes(xmss_height * 32).hex(),
    }


def _fixture_json(st: Statement, w: Witness, xmss_height: int) -> dict:
    """The rpay input format (same shape as research/m4b-groth16/fixture.json)."""
    b = w.branch
    witness = {
        "cid": w.cid.hex(), "D": w.D, "c": w.c.hex(),
        "r_open": w.r_open.hex(), "C_open": w.C_open.hex(),
        "pk_B": w.pk_B.hex(), "bal_prev": w.bal_prev,
        "bal_i": w.bal_i, "r_i": w.r_i.hex(),
        "rec_index": b.rec_index,
        "rec_path": [p.hex() for p in b.rec_path],
    }
    witness.update(_signed_fields(w, xmss_height))
    return {
        "statement": {"delta": st.delta, "N_i": st.N_i.hex(), "C_i": st.C_i.hex(),
                      "root": st.root.hex()},
        "witness": witness,
    }


def _statement_json(st: Statement) -> dict:
    return {"delta": st.delta, "N_i": st.N_i.hex(), "C_i": st.C_i.hex(),
            "root": st.root.hex()}


class RealSP1Prover:
    """Real zero-knowledge prover for R_pay (genesis + signed branches).

    `xmss_height` sizes the DUMMY auth path a genesis payment feeds into the
    signed-branch slots; it should equal the router's XMSS tree height so
    genesis and signed proofs stay shape-identical (real signed payments carry
    the countersignature's own path, whatever its height)."""

    zero_knowledge = True

    def __init__(self, bin_path: Optional[str] = None,
                 xmss_height: int = DEFAULT_XMSS_HEIGHT):
        self.bin_path = bin_path or os.environ.get("RPAY_BIN", DEFAULT_BIN)
        self.xmss_height = xmss_height
        self.last_prove_info: Optional[dict] = None  # timing/cycles of last prove
        self.last_verify_info: Optional[dict] = None

    def available(self) -> bool:
        return os.access(self.bin_path, os.X_OK)

    def _run(self, args: list, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run([self.bin_path, *args], capture_output=True,
                              text=True, timeout=timeout)

    def prove(self, st: Statement, w: Witness) -> bytes:
        # Cheap reference check first: a bad witness fails here with a named
        # constraint instead of a guest panic 25 s in.
        err = check_R_pay(st, w)
        if err:
            raise ValueError(f"cannot prove invalid statement: {err}")
        if not isinstance(w.branch, (GenesisBranch, SignedBranch)):
            raise ValueError(f"unknown branch type {type(w.branch).__name__}")
        if not self.available():
            raise RuntimeError(
                f"rpay binary not found at {self.bin_path} — build it with: "
                "cd research/m4b-groth16 && cargo build --release --bin rpay")
        with tempfile.TemporaryDirectory(prefix="rpay-prove-") as td:
            fixture = os.path.join(td, "fixture.json")
            proof = os.path.join(td, "proof.bin")
            with open(fixture, "w") as f:
                json.dump(_fixture_json(st, w, self.xmss_height), f)
            r = self._run(["prove", fixture, proof], PROVE_TIMEOUT_S)
            if r.returncode != 0:
                # returncode < 0 = killed by a signal (e.g. -9 under memory
                # pressure); stderr is usually empty then, so say so.
                raise RuntimeError(
                    f"rpay prove failed (rc={r.returncode}): "
                    f"{r.stderr.strip() or r.stdout.strip()[-400:] or '(no output)'}")
            try:
                self.last_prove_info = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                self.last_prove_info = None
            with open(proof, "rb") as f:
                proof_bytes = f.read()
        return json.dumps({
            "scheme": SCHEME,
            "proof": base64.b64encode(proof_bytes).decode(),
        }).encode()

    def verify(self, st: Statement, pi: bytes) -> bool:
        try:
            env = json.loads(pi.decode())
            if env.get("scheme") != SCHEME:
                return False
            proof_bytes = base64.b64decode(env["proof"])
        except Exception:
            return False
        if not self.available():
            raise RuntimeError(
                f"rpay binary not found at {self.bin_path} — cannot verify")
        with tempfile.TemporaryDirectory(prefix="rpay-verify-") as td:
            statement = os.path.join(td, "statement.json")
            proof = os.path.join(td, "proof.bin")
            with open(statement, "w") as f:
                json.dump(_statement_json(st), f)
            with open(proof, "wb") as f:
                f.write(proof_bytes)
            r = self._run(["verify", statement, proof], VERIFY_TIMEOUT_S)
        try:
            self.last_verify_info = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            self.last_verify_info = None
        return r.returncode == 0

    def close_values(self, st: Statement, pi: bytes) -> dict:
        raise NotImplementedError(
            "unsigned close needs the R_closeUnsigned circuit exposing "
            "(bal, N_next) as public inputs — not part of Phase 4; the payment "
            "proof deliberately hides them")
