"""Generate a real signed-branch R_pay fixture (payment #2 of a channel).

Runs payment #1 (genesis) with the fast clear prover to advance the tip to a
countersigned state, then captures payment #2's (statement, witness) — which
carries a SignedBranch (parent commitment + Bob's XMSS countersignature) — and
dumps it in the rpay fixture format. Feed the output to: rpay prove <fixture>.
"""
import json
import sys

from confetti.channel import Contract, Payer, Recipient
from confetti.relation import ClearWitnessProver, SignedBranch
from confetti.sp1 import _fixture_json

HEIGHT = int(sys.argv[2]) if len(sys.argv) > 2 else 12
PRICE = 100
DEPOSIT = 10_000

clear = ClearWitnessProver()
bob = Recipient(height=HEIGHT)
contract = Contract(prover=clear)
payer = Payer.open_on(contract, DEPOSIT, bob.pk_B, clear)

# Payment #1: genesis. Advances payer.tip to a countersigned index-1 state.
payer.pay(contract, bob, PRICE)
assert payer.tip.index == 1 and payer.tip.sigma is not None, "genesis did not settle"

# Payment #2: build its statement+witness (SignedBranch off the countersigned
# tip) without proving. Mirrors Payer.build_payment exactly.
from confetti.channel import (  # local import: keep the header clean
    Statement, Witness, new_rand, null_next, state_commit,
)

parent = payer.tip
delta = PRICE
N_i = parent.N_next
bal_i = parent.bal + delta
r_i = new_rand()
n_next = null_next(N_i, payer.c)
C_i = state_commit(payer.cid, payer.D, bal_i, n_next, r_i)
idx, path = payer.rec_index, payer.rec_path
branch = SignedBranch(parent.C, parent.r, parent.sigma, idx, path)
w = Witness(cid=payer.cid, D=payer.D, c=payer.c, r_open=payer.r_open,
            bal_prev=parent.bal, bal_i=bal_i, r_i=r_i, pk_B=payer.pk_B,
            C_open=payer.C_open, branch=branch)
st = Statement(delta, N_i, C_i, payer.root)

assert isinstance(w.branch, SignedBranch), "expected signed branch"
out = sys.argv[1] if len(sys.argv) > 1 else "signed_fixture.json"
with open(out, "w") as f:
    json.dump(_fixture_json(st, w, HEIGHT), f)
print(f"wrote {out}  (signed branch, xmss_height={HEIGHT}, index={parent.index + 1})")
