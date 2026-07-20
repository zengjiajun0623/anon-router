"""Generate a real R_pay (genesis-branch) statement+witness fixture from the
reference implementation, cross-checked against check_R_pay, for the SP1 host.

Run from anon-router root:  python3 research/m4b-groth16/make_fixture.py
Writes research/m4b-groth16/fixture.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from confetti.chain import (ChannelRecord, new_cid, new_rand, new_secret,
                            null_first, null_next, open_commit, state_commit)
from confetti.merkle import Registry
from confetti.relation import (GenesisBranch, Statement, Witness, check_R_pay)
from confetti.wots import Xmss

# --- Channel setup: Alice opens a channel with deposit D ---
cid = new_cid()
D = 1_000_000
c = new_secret()
r_open = new_rand()
C_open = open_commit(c, r_open)
pk_B = Xmss(height=2).pk

reg = Registry()
# a few decoy records so the Merkle path is non-trivial
for _ in range(5):
    reg.add(ChannelRecord(new_cid(), 7, new_secret(), new_secret()).leaf())
rec = ChannelRecord(cid, D, pk_B, C_open)
rec_index = reg.add(rec.leaf())
for _ in range(3):
    reg.add(ChannelRecord(new_cid(), 9, new_secret(), new_secret()).leaf())
root = reg.root()
rec_path = reg.proof(rec_index)

# --- First payment: delta on top of genesis (bal_prev = 0) ---
delta = 1_000
bal_prev = 0
bal_i = bal_prev + delta
N_1 = null_first(cid, c)
N_2 = null_next(N_1, c)
r_1 = new_rand()
C_1 = state_commit(cid, D, bal_i, N_2, r_1)

st = Statement(delta=delta, N_i=N_1, C_i=C_1, root=root)
w = Witness(cid=cid, D=D, c=c, r_open=r_open, bal_prev=bal_prev, bal_i=bal_i,
            r_i=r_1, pk_B=pk_B, C_open=C_open,
            branch=GenesisBranch(rec_index, rec_path))

err = check_R_pay(st, w)
assert err is None, f"fixture does not satisfy R_pay: {err}"
print("check_R_pay: OK (reference verifier accepts the fixture)")

out = {
    "statement": {"delta": delta, "N_i": N_1.hex(), "C_i": C_1.hex(),
                  "root": root.hex()},
    "witness": {"cid": cid.hex(), "D": D, "c": c.hex(), "r_open": r_open.hex(),
                "C_open": C_open.hex(), "pk_B": pk_B.hex(),
                "bal_prev": bal_prev, "bal_i": bal_i, "r_i": r_1.hex(),
                "rec_index": rec_index,
                "rec_path": [p.hex() for p in rec_path]},
}
path = os.path.join(os.path.dirname(__file__), "fixture.json")
with open(path, "w") as f:
    json.dump(out, f, indent=1)
print(f"wrote {path}")
