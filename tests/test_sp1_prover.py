"""Integration test for RealSP1Prover — the real SP1 STARK backend
(Phase 4: full disjunction, genesis + signed branches).

Gated: each proof takes ~1 min native, peaks several GB of RAM (on a 16 GB
machine run it with the router and other heavy services stopped — proving
under memory pressure gets the prover SIGKILLed), and needs the rpay binary,
so this only runs with

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


def test_sp1_genesis_and_signed_payments_end_to_end():
    prover = RealSP1Prover(xmss_height=4)
    assert prover.available(), f"rpay binary missing at {prover.bin_path}"

    contract = Contract(tau=7, prover=prover)
    bob = Recipient(height=4)
    alice = Payer.open_on(contract, D=1000, pk_B=bob.pk_B, prover=prover)

    # --- payment #1: genesis branch (real STARK) ---
    m1, pending1 = alice.build_payment(50)

    # The proof is a scheme envelope, not the witness: no secret appears in pi.
    env = json.loads(m1.pi)
    assert set(env) == {"scheme", "proof"}
    for secret in (alice.c.hex(), alice.cid.hex(), alice.r_open.hex()):
        assert secret not in m1.pi.decode()

    # Router verifies the real proof and countersigns.
    sigma1 = bob.accept(contract, m1, price=50)
    alice.on_countersign(pending1, sigma1)
    assert alice.tip.index == 1 and alice.tip.bal == 50

    # Same proof, tampered statement: must fail (statement binding).
    assert not prover.verify(Statement(51, m1.N_i, m1.C_i, m1.root), m1.pi)

    # Replay: dedup refuses to countersign the same nullifier twice.
    with pytest.raises(ValueError, match="duplicate nullifier"):
        bob.accept(contract, m1, price=50)

    # --- payment #2: SIGNED branch (Phase 4 — real STARK over the XMSS
    # countersignature on the parent state) ---
    parent = alice.tip
    m2, pending2 = alice.build_payment(50)

    # Witness hiding: neither the parent commitment nor Bob's signature bytes
    # appear in the proof, and the statement shape is identical to genesis
    # (nothing public says which branch this is).
    import base64
    raw = base64.b64decode(json.loads(m2.pi)["proof"])
    assert parent.C not in raw and parent.r not in raw
    assert parent.sigma is not None and parent.sigma.wots_sig[0] not in raw

    sigma2 = bob.accept(contract, m2, price=50)
    alice.on_countersign(pending2, sigma2)
    assert alice.tip.index == 2 and alice.tip.bal == 100

    # Signed-branch proof is also statement-bound and replay-protected.
    assert not prover.verify(Statement(51, m2.N_i, m2.C_i, m2.root), m2.pi)
    with pytest.raises(ValueError, match="duplicate nullifier"):
        bob.accept(contract, m2, price=50)
