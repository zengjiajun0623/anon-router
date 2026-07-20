"""RealSP1Prover — the SP1 STARK backend for R_pay (Phase 1: genesis branch).

Drop-in implementation of the swappable `Prover` protocol from relation.py.
Unlike ClearWitnessProver, the payment proof `pi` reveals NOTHING beyond the
public statement (delta, N_i, C_i, root): the witness (cid, c, r_open,
balances, r_i, Merkle position) stays on the client.

Mechanics
  * prove()  — shells out to the `rpay` host binary (research/m4b-groth16),
    which embeds the R_pay genesis-branch guest ELF and produces a core SP1
    STARK (~10-30 s native on an Apple-silicon laptop, ~2.8 MB). Returns a
    self-describing JSON envelope: {"scheme": ..., "proof": base64(bincode)}.
  * verify() — `rpay verify` checks the STARK against the vkey of the
    embedded guest AND that the proof's committed public values equal
    abi.encode(delta, N_i, C_i, root); statement binding is enforced in the
    binary, not here.

Scope (Phase 1): GenesisBranch only — the first payment on a channel.
SignedBranch (XMSS parent signature inside the guest) is Phase 4; proving a
non-genesis payment with this prover raises NotImplementedError.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from typing import Optional

from .relation import GenesisBranch, Statement, Witness, check_R_pay

SCHEME = "sp1-rpay-core-v1"

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BIN = os.path.join(_REPO, "research", "m4b-groth16", "target", "release", "rpay")

PROVE_TIMEOUT_S = 900
VERIFY_TIMEOUT_S = 300


def _fixture_json(st: Statement, w: Witness) -> dict:
    """The rpay input format (same shape as research/m4b-groth16/fixture.json)."""
    b = w.branch
    return {
        "statement": {"delta": st.delta, "N_i": st.N_i.hex(), "C_i": st.C_i.hex(),
                      "root": st.root.hex()},
        "witness": {"cid": w.cid.hex(), "D": w.D, "c": w.c.hex(),
                    "r_open": w.r_open.hex(), "C_open": w.C_open.hex(),
                    "pk_B": w.pk_B.hex(), "bal_prev": w.bal_prev,
                    "bal_i": w.bal_i, "r_i": w.r_i.hex(),
                    "rec_index": b.rec_index,
                    "rec_path": [p.hex() for p in b.rec_path]},
    }


def _statement_json(st: Statement) -> dict:
    return {"delta": st.delta, "N_i": st.N_i.hex(), "C_i": st.C_i.hex(),
            "root": st.root.hex()}


class RealSP1Prover:
    """Real zero-knowledge prover for the genesis branch of R_pay."""

    zero_knowledge = True

    def __init__(self, bin_path: Optional[str] = None):
        self.bin_path = bin_path or os.environ.get("RPAY_BIN", DEFAULT_BIN)
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
        if not isinstance(w.branch, GenesisBranch):
            raise NotImplementedError(
                "RealSP1Prover covers the genesis branch only (first payment); "
                "SignedBranch proving (XMSS in-guest) is Phase 4")
        if not self.available():
            raise RuntimeError(
                f"rpay binary not found at {self.bin_path} — build it with: "
                "cd research/m4b-groth16 && cargo build --release --bin rpay")
        with tempfile.TemporaryDirectory(prefix="rpay-prove-") as td:
            fixture = os.path.join(td, "fixture.json")
            proof = os.path.join(td, "proof.bin")
            with open(fixture, "w") as f:
                json.dump(_fixture_json(st, w), f)
            r = self._run(["prove", fixture, proof], PROVE_TIMEOUT_S)
            if r.returncode != 0:
                raise RuntimeError(f"rpay prove failed: {r.stderr.strip()}")
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
            "(bal, N_next) as public inputs — not part of Phase 1; the payment "
            "proof deliberately hides them")
