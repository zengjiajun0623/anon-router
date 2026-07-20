"""Integration test for RealSP1Prover — the real SP1 STARK backend (Phase 1).

Gated: proving takes ~25 s and needs the rpay binary, so this only runs with

  RUN_SP1_TESTS=1 .venv/bin/python -m pytest tests/test_sp1_prover.py -v

Build the binary first:
  cd research/m4b-groth16 && PATH="$HOME/.sp1/bin:$PATH" \
    cargo build --release --bin rpay
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from confetti.channel import Contract, Payer, Recipient  # noqa: E402
from confetti.relation import Statement  # noqa: E402
from confetti.sp1 import RealSP1Prover  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SP1_TESTS") != "1",
    reason="slow (~30 s) + needs the rpay binary; set RUN_SP1_TESTS=1",
)


def test_sp1_genesis_payment_end_to_end():
    prover = RealSP1Prover()
    assert prover.available(), f"rpay binary missing at {prover.bin_path}"

    contract = Contract(tau=7, prover=prover)
    bob = Recipient(height=4)
    alice = Payer.open_on(contract, D=1000, pk_B=bob.pk_B, prover=prover)

    m, pending = alice.build_payment(50)  # real STARK, ~25 s

    # The proof is a scheme envelope, not the witness: no secret appears in pi.
    env = json.loads(m.pi)
    assert set(env) == {"scheme", "proof"}
    for secret in (alice.c.hex(), alice.cid.hex(), alice.r_open.hex()):
        assert secret not in m.pi.decode()

    # Router verifies the real proof and countersigns.
    sigma = bob.accept(contract, m, price=50)
    alice.on_countersign(pending, sigma)
    assert alice.tip.index == 1 and alice.tip.bal == 50

    # Same proof, tampered statement: must fail (statement binding).
    assert not prover.verify(Statement(51, m.N_i, m.C_i, m.root), m.pi)

    # Replay: dedup refuses to countersign the same nullifier twice.
    with pytest.raises(ValueError, match="duplicate nullifier"):
        bob.accept(contract, m, price=50)

    # Phase-1 boundary: the second payment extends a SignedBranch parent.
    with pytest.raises(NotImplementedError, match="Phase 4"):
        alice.build_payment(50)
