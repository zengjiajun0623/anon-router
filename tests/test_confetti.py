"""Adversarial end-to-end tests for the confetti channel (Spec-v2 §4-7).

Covers the happy path plus every cheating attempt the safety theorems rule
out: dedup, stale-close→challenge→forfeit, fork inertness, overspend cap,
signature forgery, honest-close attribution-freedom, and Alice's liveness.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from confetti import (Contract, GenesisBranch, Payer, Recipient, Statement,
                      Witness, check_R_pay)
from confetti.chain import null_next, state_commit
from confetti.channel import PaymentMessage
from confetti.wots import Xmss, xmss_verify


def setup():
    c = Contract(tau=7)
    bob = Recipient(height=6)
    alice = Payer.open_on(c, D=1000, pk_B=bob.pk_B)
    return c, bob, alice


# ---- happy path -------------------------------------------------------

def test_happy_path_pay_close_settle():
    c, bob, alice = setup()
    alice.pay(c, bob, 100)
    alice.pay(c, bob, 250)               # cumulative 350
    tip = alice.tip
    assert tip.bal == 350 and tip.index == 2 and tip.sigma is not None
    c.close_signed(alice.cid, tip, now=10)
    to_bob, to_alice = c.settle(alice.cid, now=20)
    assert (to_bob, to_alice) == (350, 650)


def test_genesis_close_full_refund():
    c, bob, alice = setup()
    c.close_genesis(alice.cid, alice.c, alice.r_open, now=5)
    assert c.settle(alice.cid, now=15) == (0, 1000)


# ---- payment proof soundness -----------------------------------------

def test_payment_proof_verifies_and_binds():
    c, bob, alice = setup()
    m, pending = alice.build_payment(100)
    st = Statement(m.delta, m.N_i, m.C_i, c.root())
    assert alice.prover.verify(st, m.pi)
    # tampering with delta breaks verification against the published C_i
    bad = Statement(999, m.N_i, m.C_i, c.root())
    assert not alice.prover.verify(bad, m.pi)


def test_overspend_capped_at_D():
    c, bob, alice = setup()
    with pytest.raises(ValueError):
        alice.build_payment(1001)        # delta > D → unprovable
    alice.pay(c, bob, 1000)              # exactly D is fine
    with pytest.raises(ValueError):
        alice.build_payment(1)           # any more overspends


# ---- dedup / fork -----------------------------------------------------

def test_dedup_refuses_second_reveal_of_same_nullifier():
    c, bob, alice = setup()
    alice.pay(c, bob, 100)
    # Alice tries to fork: build a *sibling* off the genesis again by resetting
    # her tip to genesis. Its reveal N_1 was already seen by Bob.
    genesis_tip = alice.tip
    alice.tip = _genesis_of(alice)
    m2, _ = alice.build_payment(50)
    with pytest.raises(ValueError, match="duplicate nullifier"):
        bob.accept(c, m2, price=50)


def _genesis_of(alice):
    from confetti.channel import SignedState
    from confetti.chain import null_first
    return SignedState(0, 0, b"", b"", null_first(alice.cid, alice.c), None, b"")


# ---- stale close → challenge → forfeit --------------------------------

def test_stale_close_is_challenged_and_forfeits():
    c, bob, alice = setup()
    s1 = _clone(alice.pay(c, bob, 100))   # state 1, Bob holds message m1
    alice.pay(c, bob, 200)                # state 2 (the true tip)
    # Alice cheats: closes on the *stale* signed state 1 (bal 100, not 300).
    c.close_signed(alice.cid, s1, now=10)
    # Bob holds m2 whose revealed N_2 == s1.N_next (the exhibited nullifier).
    m2 = bob.inbox[1]
    assert c.challenge(alice.cid, m2, now=11) is True
    assert c.settle(alice.cid, now=20) == (1000, 0)   # Alice forfeits everything


def test_honest_close_cannot_be_challenged():
    c, bob, alice = setup()
    alice.pay(c, bob, 100)
    tip = _clone(alice.pay(c, bob, 200))
    c.close_signed(alice.cid, tip, now=10)
    # Bob tries every message he holds; none reveal the exhibited N_{x+1}.
    assert all(c.challenge(alice.cid, m, now=11) is False for m in bob.inbox)
    assert c.settle(alice.cid, now=20) == (300, 700)


def test_challenge_after_window_rejected():
    c, bob, alice = setup()
    s1 = _clone(alice.pay(c, bob, 100))
    alice.pay(c, bob, 200)
    c.close_signed(alice.cid, s1, now=10)
    assert c.challenge(alice.cid, bob.inbox[1], now=10 + c.tau + 1) is False


# ---- liveness ---------------------------------------------------------

def test_awol_recipient_timeout_forfeit_to_bob():
    # If Alice never closes, Bob claims the deposit (§4 timers).
    c, bob, alice = setup()
    alice.pay(c, bob, 100)
    assert c.timeout_forfeit(alice.cid) == (1000, 0)


def test_alice_can_always_genesis_close_if_bob_never_signs():
    # Bob withholds his countersignature; Alice still exits via genesis refund.
    c, bob, alice = setup()
    m, pending = alice.build_payment(100)   # never handed to Bob
    c.close_genesis(alice.cid, alice.c, alice.r_open, now=5)
    assert c.settle(alice.cid, now=15) == (0, 1000)


# ---- signature layer --------------------------------------------------

def test_xmss_sign_verify_and_forgery():
    x = Xmss(height=4)
    msg = os.urandom(32)
    sig = x.sign(msg)
    assert xmss_verify(x.pk, msg, sig)
    assert not xmss_verify(x.pk, os.urandom(32), sig)   # wrong message
    assert not xmss_verify(os.urandom(32), msg, sig)    # wrong root


def test_forged_countersignature_rejected_by_payer():
    c, bob, alice = setup()
    m, pending = alice.build_payment(100)
    forged = Xmss(height=4).sign(pending.C)     # signed under a different root
    import pytest as _p
    with _p.raises(AssertionError):
        alice.on_countersign(pending, forged)


def _clone(state):
    from confetti.channel import SignedState
    return SignedState(state.index, state.bal, state.C, state.r,
                       state.N_next, state.sigma, state.N_reveal)
