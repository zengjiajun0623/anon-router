"""The confetti channel state machine (Spec-v2 §4-6): Payer (Alice), Recipient
(Bob / the router), and Contract (the on-chain referee — modeled here as a
Python object for the off-chain M4a milestone; M4b replaces it with a real
contract, same interface).

Trust properties this delivers off-chain already:
  * Bob never signs two messages revealing the same nullifier (dedup) → an
    unsigned frontier is at most one message deep.
  * A stale close (closing a non-tip state) is caught by nullifier collision
    against a message Bob holds → challenge → Alice forfeits.
  * bal <= D enforced in every payment/close proof → Bob can't be paid past
    the deposit, Alice can't recover money already paid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .chain import (ChannelRecord, new_cid, new_rand, new_secret, null_first,
                    null_next, open_commit, state_commit)
from .merkle import Registry
from .relation import (ClearWitnessProver, GenesisBranch, SignedBranch,
                       Statement, Witness)
from .wots import Xmss, XmssSignature, xmss_verify


# ---------- messages ----------

@dataclass
class PaymentMessage:
    N_i: bytes
    delta: int
    C_i: bytes
    pi: bytes
    root: bytes         # the registry root this payment was proved against


@dataclass
class SignedState:
    index: int
    bal: int
    C: bytes
    r: bytes
    N_next: bytes       # the next-nullifier committed in C
    sigma: Optional[XmssSignature]  # Bob's countersignature (None until signed)
    N_reveal: bytes     # the nullifier this state revealed when created


# ---------- contract (referee) ----------

@dataclass
class CloseRecord:
    cid: bytes
    mode: str                 # 'genesis' | 'signed' | 'unsigned'
    bal: int
    exhibit: set              # E
    C_x: Optional[bytes]      # published only for unsigned closes
    opened_at: int
    challenged: bool = False


class Contract:
    def __init__(self, tau: int = 7, prover: Optional[ClearWitnessProver] = None):
        self.registry = Registry()
        self.channels: dict[bytes, ChannelRecord] = {}
        self.closes: dict[bytes, CloseRecord] = {}
        self.tau = tau
        self.prover = prover or ClearWitnessProver()
        self.settled: dict[bytes, tuple[int, int]] = {}  # cid -> (to_bob, to_alice)
        self._roots: set = {self.registry.root()}        # every root ever published

    def root(self) -> bytes:
        return self.registry.root()

    def accepts_root(self, r: bytes) -> bool:
        """Spec §3: verifiers accept recent epoch roots. The referee keeps the
        full history so a membership path proved at open stays valid."""
        return r in self._roots

    # open ---------------------------------------------------------------
    def open(self, rec: ChannelRecord) -> int:
        if rec.cid in self.channels:
            raise ValueError("cid already opened")  # registry cid-uniqueness (BUG 5)
        self.channels[rec.cid] = rec
        idx = self.registry.add(rec.leaf())
        self._roots.add(self.registry.root())
        return idx

    # close --------------------------------------------------------------
    def close_genesis(self, cid: bytes, c: bytes, r_open: bytes, now: int) -> None:
        rec = self.channels[cid]
        assert open_commit(c, r_open) == rec.C_open, "C_open mismatch"
        N_1 = null_first(cid, c)
        self._record_close(cid, "genesis", 0, {N_1}, None, now)

    def close_signed(self, cid: bytes, state: SignedState, now: int) -> None:
        rec = self.channels[cid]
        assert state.sigma is not None, "signed close needs a countersignature"
        assert state_commit(cid, rec.D, state.bal, state.N_next, state.r) == state.C
        assert xmss_verify(rec.pk_B, state.C, state.sigma), "bad Bob signature"
        assert state.bal <= rec.D
        self._record_close(cid, "signed", state.bal, {state.N_next}, None, now)

    def close_unsigned(self, cid: bytes, st: Statement, pi: bytes, now: int) -> None:
        assert self.prover.verify(st, pi), "unsigned close proof invalid"
        # The payout balance and exhibit nullifiers are derived from the proof,
        # never taken from the caller (review BUG 1). N_x is the revealed public
        # input st.N_i; bal_x and N_{x+1} are bound to C_x by the relation.
        v = self.prover.close_values(st, pi)
        self._record_close(cid, "unsigned", v["bal"], {st.N_i, v["n_next"]}, st.C_i, now)

    def _record_close(self, cid, mode, bal, exhibit, C_x, now):
        assert cid not in self.closes, "channel already closed"
        self.closes[cid] = CloseRecord(cid, mode, bal, exhibit, C_x, now)

    # challenge ----------------------------------------------------------
    def challenge(self, cid: bytes, m: PaymentMessage, now: int) -> bool:
        cr = self.closes[cid]
        if now > cr.opened_at + self.tau:
            return False  # window elapsed
        if not self.accepts_root(m.root):
            return False
        st = Statement(m.delta, m.N_i, m.C_i, m.root)
        if not self.prover.verify(st, m.pi):          # (1) proof validity
            return False
        if cr.mode == "unsigned" and m.C_i == cr.C_x:  # (2) same-state exception
            return False
        if m.N_i not in cr.exhibit:                    # (3) collision with E
            return False
        cr.challenged = True
        return True

    # settle -------------------------------------------------------------
    def settle(self, cid: bytes, now: int) -> tuple[int, int]:
        cr = self.closes[cid]
        D = self.channels[cid].D
        assert now > cr.opened_at + self.tau, "challenge window still open"
        result = (D, 0) if cr.challenged else (cr.bal, D - cr.bal)
        self.settled[cid] = result
        return result

    def timeout_forfeit(self, cid: bytes) -> tuple[int, int]:
        """Bob claims the whole deposit if Alice never closed (§4 timers)."""
        assert cid not in self.closes, "channel has a pending/final close"
        D = self.channels[cid].D
        self.settled[cid] = (D, 0)
        return (D, 0)


# ---------- recipient (Bob / router) ----------

class Recipient:
    def __init__(self, height: int = 10):
        self.xmss = Xmss(height)
        self.pk_B = self.xmss.pk
        self.seen: set = set()                 # global nullifier dedup
        self.inbox: list[PaymentMessage] = []  # challenge evidence

    def accept(self, contract: Contract, m: PaymentMessage, price: int) -> XmssSignature:
        if not contract.accepts_root(m.root):
            raise ValueError("payment cites an unrecognized root")
        st = Statement(m.delta, m.N_i, m.C_i, m.root)
        if not contract.prover.verify(st, m.pi):
            raise ValueError("payment proof invalid")
        if m.delta != price:
            raise ValueError(f"delta {m.delta} != price {price}")
        if m.N_i in self.seen:
            raise ValueError("duplicate nullifier — refusing to countersign")
        self.seen.add(m.N_i)
        self.inbox.append(m)
        return self.xmss.sign(m.C_i)


# ---------- payer (Alice / wallet) ----------

class Payer:
    def __init__(self, D: int, pk_B: bytes,
                 prover: Optional[ClearWitnessProver] = None):
        self.prover = prover or ClearWitnessProver()
        self.D = D
        self.pk_B = pk_B
        self.c = new_secret()
        self.r_open = new_rand()
        self.cid = new_cid()
        self.C_open = open_commit(self.c, self.r_open)
        self.rec = ChannelRecord(self.cid, D, pk_B, self.C_open)
        self.rec_index: Optional[int] = None
        self.rec_path: list = []
        self.root: bytes = b""
        # genesis: index 0, balance 0, committed next-nullifier N_1
        self.tip = SignedState(
            index=0, bal=0, C=b"", r=b"",
            N_next=null_first(self.cid, self.c), sigma=None,
            N_reveal=b"",
        )

    def register(self, rec_index: int, rec_path: list, root: bytes) -> None:
        """Record the registry position and root returned by the contract."""
        self.rec_index = rec_index
        self.rec_path = rec_path
        self.root = root

    @classmethod
    def open_on(cls, contract: Contract, D: int, pk_B: bytes,
                prover: Optional[ClearWitnessProver] = None) -> "Payer":
        """Convenience for the in-process case: open directly on a Contract."""
        p = cls(D, pk_B, prover)
        idx = contract.open(p.rec)
        p.register(idx, contract.registry.proof(idx), contract.root())
        return p

    def _pk_membership(self):
        if self.rec_index is None:
            raise RuntimeError("payer not registered on a contract")
        return self.rec_index, self.rec_path

    def build_payment(self, delta: int) -> tuple[PaymentMessage, SignedState]:
        parent = self.tip
        N_i = parent.N_next                     # reveal parent's committed next
        bal_i = parent.bal + delta
        r_i = new_rand()
        n_next = null_next(N_i, self.c)
        C_i = state_commit(self.cid, self.D, bal_i, n_next, r_i)
        idx, path = self._pk_membership()
        if parent.index == 0:
            branch = GenesisBranch(idx, path)
        else:
            branch = SignedBranch(parent.C, parent.r, parent.sigma, idx, path)
        w = Witness(cid=self.cid, D=self.D, c=self.c, r_open=self.r_open,
                    bal_prev=parent.bal, bal_i=bal_i, r_i=r_i, pk_B=self.pk_B,
                    C_open=self.C_open, branch=branch)
        st = Statement(delta, N_i, C_i, self.root)
        pi = self.prover.prove(st, w)
        m = PaymentMessage(N_i, delta, C_i, pi, self.root)
        pending = SignedState(parent.index + 1, bal_i, C_i, r_i, n_next, None, N_i)
        return m, pending

    def on_countersign(self, pending: SignedState, sigma: XmssSignature) -> None:
        assert xmss_verify(self.pk_B, pending.C, sigma), "bad countersignature"
        pending.sigma = sigma
        self.tip = pending

    def pay(self, contract: Contract, bob: Recipient, delta: int) -> SignedState:
        m, pending = self.build_payment(delta)
        sigma = bob.accept(contract, m, price=delta)
        self.on_countersign(pending, sigma)
        return self.tip
