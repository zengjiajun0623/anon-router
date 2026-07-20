/-!
# `ConfettiChannels.sol` — state-machine model and machine-checked safety proofs

Model of `anon-router/contracts/src/ConfettiChannels.sol` (Spec-v2 §4–§6) as a
per-channel transition system, with the §7 on-chain settlement theorems proved.

## Scope and identifications

* **One channel.** Channels in the contract are fully independent: every
  storage word touched by `open/close*/challenge/finalize/timeoutForfeit` is
  keyed by `cid`, `usedCid` makes a `cid` single-use forever, and `_credit`
  only *adds* to the pull-payment ledger. The per-channel invariants proved
  here therefore sum over channels: conservation of each channel's `D` gives
  conservation of the whole contract balance.
* **Roles, not addresses.** `alice`/`bob` are the two payout roles of the
  channel. `wBob/wAlice` is the channel's contribution to
  `withdrawable[bob]/withdrawable[alice]`; `pBob/pAlice` the cumulative ETH
  actually transferred out by `withdraw()` on that contribution. If the two
  roles share an address the contract merges the ledger slots; the per-channel
  sums proved here are unchanged.
* **Verifier over-approximated.** `IVerifier` acceptance is an *arbitrary*
  predicate in `Params`. Every theorem below holds for every verifier
  behaviour, including a completely broken one that accepts everything — the
  on-chain safety layer does not rest on proof soundness (soundness is what
  makes *challenge evidence* meaningful; that layer is proved in
  `zk-payments-confetti/lean`, Spec-v2 §7 evidence characterization).
* **`bytes32`/`bytes16` as `Nat`, wei as `Nat`, `block.timestamp` as a
  monotone `now`.** Solidity `uint256` arithmetic here never overflows in the
  contract (checked arithmetic; `bal <= D` guards the one subtraction), so
  `Nat` is faithful.
* **Merkle registry / epoch roots elided.** `rootAccepted` only gates whether
  a close/challenge proof is accepted, i.e. it is part of the verifier
  over-approximation above; it moves no funds.

## Field ↔ contract map

| model                | contract                                            |
|----------------------|-----------------------------------------------------|
| `opened`             | `usedCid[cid]` (set once, never cleared)            |
| `chExists`           | `channels[cid].exists`                              |
| `deposit`            | `channels[cid].deposit` (survives `exists := false`)|
| `chOpenedAt`         | `channels[cid].openedAt`                            |
| `reqCloseAt`         | `channels[cid].reqCloseAt` (`none` ≡ `0`)           |
| `close`              | `closes[cid]` (`none` ≡ `mode == NONE`)             |
| `wBob`,`wAlice`      | this channel's share of `withdrawable[·]`           |
| `pBob`,`pAlice`      | ETH paid out by `withdraw()` from that share        |
| `ethBal`             | this channel's share of `address(this).balance`     |

## Theorems (all `sorry`-free)

1. `conservation`, `solvency`, `settlement_conservation` — escrowed ETH +
   credited ledger always account for exactly `D`; the contract can always
   honor the ledger.
2. `finalize_payout` (+ challenged/unchallenged corollaries) — the payout
   split is exactly (`D`,`0`) after a challenge and (`bal`,`D−bal`) otherwise.
3. `finalized_absorbing`, `challenged_absorbing`, `settled_credits_frozen`,
   `withdrawn_bounded` — settlement is terminal: credits happen exactly once,
   nothing can re-finalize, re-challenge, or over-withdraw.
4. `unchallenged_close_settles` — liveness flavour: from any reachable state
   with a pending unchallenged close, the clock plus one `finalize` call
   settles the full deposit as (`bal`, `D−bal`).

One disclosed quirk, proved harmless: after `timeoutForfeit` the contract
leaves `closes[cid].openedAt = 0` and `challenged = false`, so on a chain
where `block.timestamp ≤ tau` a `challenge` could still flip the flag. The
close is already `finalized`, so `settled_credits_frozen` shows the flip is
financially inert (no credit ever moves again).
-/

namespace Zkpc.Confetti

/-- Close modes; the contract's `CloseMode.NONE` is `Option.none` on `St.close`. -/
inductive Mode where
  | genesis
  | signed
  | unsigned
  deriving Repr, DecidableEq

/-- The contract's `Close` struct (minus the redundant `NONE` mode).
`exhibitB`/`cX` use `Option` for the contract's `bytes32(0)` sentinels. -/
structure CloseRec where
  mode : Mode
  bal : Nat
  exhibitA : Nat
  exhibitB : Option Nat
  cX : Option Nat
  t0 : Nat
  challenged : Bool
  finalized : Bool
  deriving Repr

/-- Per-channel contract state (see the field map in the module docstring). -/
structure St where
  now : Nat
  opened : Bool
  chExists : Bool
  deposit : Nat
  chOpenedAt : Nat
  reqCloseAt : Option Nat
  close : Option CloseRec
  wBob : Nat
  wAlice : Nat
  pBob : Nat
  pAlice : Nat
  ethBal : Nat
  deriving Repr

/-- State before `open(cid, …)` was ever called. -/
def St.init : St :=
  { now := 0, opened := false, chExists := false, deposit := 0, chOpenedAt := 0,
    reqCloseAt := none, close := none, wBob := 0, wAlice := 0, pBob := 0,
    pAlice := 0, ethBal := 0 }

/-- Protocol parameters: the three immutable timers plus the verifier,
over-approximated as arbitrary acceptance predicates (safety below holds for
*every* choice, i.e. even against a verifier that accepts garbage). Argument
tuples mirror the contract's public inputs that vary per call. -/
structure Params where
  tau : Nat
  tAbs : Nat
  tReq : Nat
  /-- `verifyGenesisClose` acceptance, abstracted over `n₁`. -/
  genesisOk : Nat → Prop
  /-- `verifySignedClose` acceptance, over `(nNext, bal, D)`. -/
  signedOk : Nat → Nat → Nat → Prop
  /-- `verifyCloseUnsigned` acceptance, over `(cX, nX, nNext, bal, delta)`. -/
  unsignedOk : Nat → Nat → Nat → Nat → Nat → Prop
  /-- `verifyPayment` acceptance, over `(nM, cM)`. -/
  paymentOk : Nat → Nat → Prop

/-- `finalize`'s `toBob`: the whole deposit if challenged, else the claim. -/
def payBob (D : Nat) (cl : CloseRec) : Nat := if cl.challenged then D else cl.bal

/-- `finalize`'s `toAlice = D - toBob`. -/
def payAlice (D : Nat) (cl : CloseRec) : Nat := D - payBob D cl

/-- The `closes[cid]` record `timeoutForfeit` leaves behind: default struct
with `mode := SIGNED, finalized := true` (contract lines 279–280). Note
`t0 = 0` and `challenged = false` are the defaults — the source of the
disclosed inert-challenge quirk. -/
def timeoutRec : CloseRec :=
  { mode := .signed, bal := 0, exhibitA := 0, exhibitB := none, cX := none,
    t0 := 0, challenged := false, finalized := true }

/-- Post-state of `finalize(cid)`: mark finalized, drop the channel, credit
the pull-payment ledger with the (`toBob`, `toAlice`) split. -/
def St.finalizeSt (s : St) (cl : CloseRec) : St :=
  { s with chExists := false,
           close := some { cl with finalized := true },
           wBob := s.wBob + payBob s.deposit cl,
           wAlice := s.wAlice + payAlice s.deposit cl }

/-- One transition per external function of the contract (plus `tick` for the
passage of block time). Each constructor's hypotheses are exactly the
`require`s of the corresponding Solidity function, in source order; access
control (`msg.sender == alice/bob`) needs no model content because senders are
roles here. -/
inductive Step (P : Params) : St → St → Prop where
  /-- Block time advances (monotonically). -/
  | tick {s : St} (t : Nat) (ht : s.now ≤ t) :
      Step P s { s with now := t }
  /-- `open`: `!usedCid[cid]`, `msg.value > 0`. Escrows the deposit. -/
  | openChannel {s : St} (D : Nat) (hD : 0 < D) (hcid : s.opened = false) :
      Step P s { s with opened := true, chExists := true, deposit := D,
                        chOpenedAt := s.now, ethBal := s.ethBal + D }
  /-- `requestClose`: Bob starts the `T_req` clock, once. -/
  | requestClose {s : St} (hex : s.chExists = true) (hreq : s.reqCloseAt = none) :
      Step P s { s with reqCloseAt := some s.now }
  /-- `closeGenesis`: full-refund close, `bal = 0`, exhibit set `{N₁}`. -/
  | closeGenesis {s : St} (n1 : Nat) (hex : s.chExists = true)
      (hnone : s.close = none) (hpf : P.genesisOk n1) :
      Step P s { s with close := some ⟨.genesis, 0, n1, none, none, s.now, false, false⟩ }
  /-- `closeSigned`: `bal ≤ D` enforced, exhibit set `{N_{x+1}}`. -/
  | closeSigned {s : St} (nNext bal : Nat) (hex : s.chExists = true)
      (hnone : s.close = none) (hbal : bal ≤ s.deposit)
      (hpf : P.signedOk nNext bal s.deposit) :
      Step P s { s with close := some ⟨.signed, bal, nNext, none, none, s.now, false, false⟩ }
  /-- `closeUnsigned`: `bal ≤ D` enforced, publishes `C_x`, exhibit set
  `{N_x, N_{x+1}}` (`exhibitA := nNext, exhibitB := nX` as in `_startClose`). -/
  | closeUnsigned {s : St} (cX nX nNext bal delta : Nat) (hex : s.chExists = true)
      (hnone : s.close = none) (hbal : bal ≤ s.deposit)
      (hpf : P.unsignedOk cX nX nNext bal delta) :
      Step P s { s with close := some ⟨.unsigned, bal, nNext, some nX, some cX, s.now, false, false⟩ }
  /-- `challenge`: inside the window, unchallenged, same-state exception for
  unsigned closes, nullifier collision with the exhibit set (Spec §5). -/
  | challenge {s : St} {cl : CloseRec} (nM cM : Nat) (hc : s.close = some cl)
      (hnch : cl.challenged = false) (hwin : s.now ≤ cl.t0 + P.tau)
      (hpf : P.paymentOk nM cM)
      (hss : cl.mode = .unsigned → some cM ≠ cl.cX)
      (hcol : nM = cl.exhibitA ∨ cl.exhibitB = some nM) :
      Step P s { s with close := some { cl with challenged := true } }
  /-- `finalize`: any party, after the window, once. -/
  | finalize {s : St} {cl : CloseRec} (hc : s.close = some cl)
      (hnf : cl.finalized = false) (hwin : cl.t0 + P.tau < s.now) :
      Step P s (s.finalizeSt cl)
  /-- `timeoutForfeit`: no close pending, a deadline passed ⇒ Bob gets `D`. -/
  | timeoutForfeit {s : St} (hex : s.chExists = true) (hnone : s.close = none)
      (hdl : s.chOpenedAt + P.tAbs < s.now ∨
             ∃ r, s.reqCloseAt = some r ∧ r + P.tReq < s.now) :
      Step P s { s with chExists := false, close := some timeoutRec,
                        wBob := s.wBob + s.deposit }
  /-- `withdraw()` by Bob: zero the slot, pay it out. -/
  | withdrawBob {s : St} (h : 0 < s.wBob) :
      Step P s { s with wBob := 0, pBob := s.pBob + s.wBob,
                        ethBal := s.ethBal - s.wBob }
  /-- `withdraw()` by Alice: zero the slot, pay it out. -/
  | withdrawAlice {s : St} (h : 0 < s.wAlice) :
      Step P s { s with wAlice := 0, pAlice := s.pAlice + s.wAlice,
                        ethBal := s.ethBal - s.wAlice }

/-- Reachability from the pre-`open` state. -/
inductive Reachable (P : Params) : St → Prop where
  | init : Reachable P St.init
  | step {s s' : St} : Reachable P s → Step P s s' → Reachable P s'

/-- Total amount settlement has moved from escrow to the ledger: `D` once the
close is finalized, `0` before. -/
def settledAmt (s : St) : Nat :=
  match s.close with
  | some cl => if cl.finalized then s.deposit else 0
  | none => 0

/-- The inductive invariant. `conserve` + `credited` together are the
accounting core: escrow + paid-out always equals the deposit, and the ledger
is fed exactly once, by settlement, with exactly `D`. -/
structure Inv (s : St) : Prop where
  /-- Escrowed ETH plus everything ever withdrawn equals the deposit. -/
  conserve : s.ethBal + s.pBob + s.pAlice = (if s.opened then s.deposit else 0)
  /-- Ledger credits (pending + already withdrawn) equal `settledAmt`:
  `0` before settlement, `D` after — credits happen exactly once. -/
  credited : s.wBob + s.pBob + s.wAlice + s.pAlice = settledAmt s
  /-- Any recorded close belongs to an opened channel and claims `bal ≤ D`. -/
  closeInv : ∀ cl : CloseRec, s.close = some cl → s.opened = true ∧ cl.bal ≤ s.deposit
  /-- Before `open` nothing exists. -/
  virgin : s.opened = false → s.close = none ∧ s.chExists = false ∧ s.deposit = 0

/-- `settledAmt` never exceeds the (opened-guarded) deposit. -/
theorem settledAmt_le {s : St} (h : Inv s) :
    settledAmt s ≤ (if s.opened then s.deposit else 0) := by
  cases hc : s.close with
  | none => simp [settledAmt, hc]
  | some cl =>
      have ⟨ho, _⟩ := h.closeInv cl hc
      cases hf : cl.finalized <;> simp [settledAmt, hc, hf, ho]

/-- A channel that exists has been opened. -/
theorem opened_of_exists {s : St} (h : Inv s) (hex : s.chExists = true) :
    s.opened = true := by
  cases hop : s.opened with
  | true => rfl
  | false => have := (h.virgin hop).2.1; rw [this] at hex; cases hex

/-- The invariant holds initially. -/
theorem Inv.initial : Inv St.init := by
  refine ⟨rfl, rfl, ?_, ?_⟩
  · intro cl hcl; cases hcl
  · intro _; exact ⟨rfl, rfl, rfl⟩

/-- The invariant is preserved by every transition. -/
theorem Inv.preserved {P : Params} {s s' : St} (h : Inv s) (st : Step P s s') :
    Inv s' := by
  cases st with
  | tick t ht =>
      exact ⟨h.conserve, h.credited, h.closeInv, h.virgin⟩
  | openChannel D hD hcid =>
      have hv := h.virgin hcid
      have hcons := h.conserve
      have hcred := h.credited
      simp only [hcid, if_neg Bool.false_ne_true] at hcons
      refine ⟨?_, ?_, ?_, ?_⟩
      · simp; omega
      · simp only [settledAmt, hv.1] at hcred ⊢; omega
      · intro cl hcl; simp only [hv.1] at hcl; cases hcl
      · intro hop; cases hop
  | requestClose hex hreq =>
      exact ⟨h.conserve, h.credited, h.closeInv, h.virgin⟩
  | closeGenesis n1 hex hnone hpf =>
      have ho := opened_of_exists h hex
      have hcred := h.credited
      refine ⟨h.conserve, ?_, ?_, ?_⟩
      · simp only [settledAmt, hnone] at hcred
        simp only [settledAmt, if_neg Bool.false_ne_true]; omega
      · intro cl hcl
        cases hcl; exact ⟨ho, Nat.zero_le _⟩
      · intro hop; rw [hop] at ho; cases ho
  | closeSigned nNext bal hex hnone hbal hpf =>
      have ho := opened_of_exists h hex
      have hcred := h.credited
      refine ⟨h.conserve, ?_, ?_, ?_⟩
      · simp only [settledAmt, hnone] at hcred
        simp only [settledAmt, if_neg Bool.false_ne_true]; omega
      · intro cl hcl
        cases hcl; exact ⟨ho, hbal⟩
      · intro hop; rw [hop] at ho; cases ho
  | closeUnsigned cX nX nNext bal delta hex hnone hbal hpf =>
      have ho := opened_of_exists h hex
      have hcred := h.credited
      refine ⟨h.conserve, ?_, ?_, ?_⟩
      · simp only [settledAmt, hnone] at hcred
        simp only [settledAmt, if_neg Bool.false_ne_true]; omega
      · intro cl hcl
        cases hcl; exact ⟨ho, hbal⟩
      · intro hop; rw [hop] at ho; cases ho
  | challenge nM cM hc hnch hwin hpf hss hcol =>
      have hcred := h.credited
      have hci := h.closeInv _ hc
      refine ⟨h.conserve, ?_, ?_, ?_⟩
      · simp only [settledAmt, hc] at hcred
        simp only [settledAmt]; exact hcred
      · intro cl' hcl'; cases hcl'; exact hci
      · intro hop; have := (h.virgin hop).1; rw [this] at hc; cases hc
  | finalize hc hnf hwin =>
      rename_i cl
      have hcred := h.credited
      have ⟨ho, hbal⟩ := h.closeInv _ hc
      simp only [settledAmt, hc, hnf, if_neg Bool.false_ne_true] at hcred
      refine ⟨?_, ?_, ?_, ?_⟩
      · exact h.conserve
      · simp only [St.finalizeSt, settledAmt, payBob, payAlice]
        cases cl.challenged <;> simp <;> omega
      · intro cl' hcl'
        simp only [St.finalizeSt] at hcl'
        cases hcl'; exact ⟨ho, hbal⟩
      · intro hop
        have hop' : s.opened = false := hop
        rw [hop'] at ho; cases ho
  | timeoutForfeit hex hnone hdl =>
      have ho := opened_of_exists h hex
      have hcred := h.credited
      simp only [settledAmt, hnone] at hcred
      refine ⟨h.conserve, ?_, ?_, ?_⟩
      · simp [settledAmt, timeoutRec]; omega
      · intro cl hcl; cases hcl; exact ⟨ho, Nat.zero_le _⟩
      · intro hop; rw [hop] at ho; cases ho
  | withdrawBob hw =>
      have hcons := h.conserve
      have hcred := h.credited
      have hle := settledAmt_le h
      have ho : s.opened = true := by
        cases hop : s.opened with
        | true => rfl
        | false => simp only [hop, if_neg Bool.false_ne_true] at hle; omega
      simp only [ho] at hcons hle
      refine ⟨?_, ?_, h.closeInv, ?_⟩
      · simp only [ho]; omega
      · simp only [settledAmt]
        simp only [settledAmt] at hcred; omega
      · intro hop; rw [hop] at ho; cases ho
  | withdrawAlice hw =>
      have hcons := h.conserve
      have hcred := h.credited
      have hle := settledAmt_le h
      have ho : s.opened = true := by
        cases hop : s.opened with
        | true => rfl
        | false => simp only [hop, if_neg Bool.false_ne_true] at hle; omega
      simp only [ho] at hcons hle
      refine ⟨?_, ?_, h.closeInv, ?_⟩
      · simp only [ho]; omega
      · simp only [settledAmt]
        simp only [settledAmt] at hcred; omega
      · intro hop; rw [hop] at ho; cases ho

/-- Every reachable state satisfies the invariant. -/
theorem reachable_inv {P : Params} {s : St} (h : Reachable P s) : Inv s := by
  induction h with
  | init => exact Inv.initial
  | step _ st ih => exact ih.preserved st

/-! ## Theorem 1 — conservation -/

/-- **Conservation.** For any reachable state of an opened channel, the ETH
still escrowed plus everything already paid out equals the deposit `D`:
settlement neither creates nor destroys ETH. -/
theorem conservation {P : Params} {s : St} (hr : Reachable P s)
    (ho : s.opened = true) : s.ethBal + s.pBob + s.pAlice = s.deposit := by
  have h := (reachable_inv hr).conserve
  rwa [ho, if_pos rfl] at h

/-- **Solvency.** The pending ledger credits never exceed the escrowed ETH:
`withdraw()` can always be honored, for both parties, in any order. -/
theorem solvency {P : Params} {s : St} (hr : Reachable P s) :
    s.wBob + s.wAlice ≤ s.ethBal := by
  have h := reachable_inv hr
  have hcons := h.conserve
  have hcred := h.credited
  have hle := settledAmt_le h
  cases hop : s.opened <;> simp only [hop] at hcons hle <;> omega

/-- **Conservation through settlement.** Once the close is finalized, the
ledger credits attributable to the channel (pending + withdrawn) sum to
exactly `D` — forever after, through any number of withdrawals. -/
theorem settlement_conservation {P : Params} {s : St} {cl : CloseRec}
    (hr : Reachable P s) (hc : s.close = some cl) (hf : cl.finalized = true) :
    (s.wBob + s.pBob) + (s.wAlice + s.pAlice) = s.deposit := by
  have h := (reachable_inv hr).credited
  simp [settledAmt, hc, hf] at h
  omega

/-! ## Theorem 2 — payout correctness -/

/-- **Payout correctness.** Firing `finalize` on a reachable state credits
Bob exactly `if challenged then D else bal` and Alice exactly the remainder,
and the two credits split `D` exactly. -/
theorem finalize_payout {P : Params} {s : St} {cl : CloseRec}
    (hr : Reachable P s) (hc : s.close = some cl) (hnf : cl.finalized = false) :
    (s.finalizeSt cl).wBob = (if cl.challenged then s.deposit else cl.bal) ∧
    (s.finalizeSt cl).wAlice = s.deposit - (if cl.challenged then s.deposit else cl.bal) ∧
    (s.finalizeSt cl).wBob + (s.finalizeSt cl).wAlice = s.deposit := by
  have h := reachable_inv hr
  have hcred := h.credited
  have ⟨_, hbal⟩ := h.closeInv _ hc
  simp only [settledAmt, hc, hnf, if_neg Bool.false_ne_true] at hcred
  simp only [St.finalizeSt, payBob, payAlice]
  cases cl.challenged <;> simp <;> omega

/-- **No theft (challenged).** A challenged close forfeits the whole deposit:
Bob is credited `D`, Alice `0`. -/
theorem finalize_payout_challenged {P : Params} {s : St} {cl : CloseRec}
    (hr : Reachable P s) (hc : s.close = some cl) (hnf : cl.finalized = false)
    (hch : cl.challenged = true) :
    (s.finalizeSt cl).wBob = s.deposit ∧ (s.finalizeSt cl).wAlice = 0 := by
  have ⟨h1, h2, _⟩ := finalize_payout hr hc hnf
  rw [hch] at h1 h2
  simp at h1 h2
  exact ⟨h1, h2⟩

/-- **No theft (unchallenged).** An unchallenged close pays Bob exactly the
claimed balance (which the contract has already bounded by `D`) and refunds
Alice exactly `D − bal`. -/
theorem finalize_payout_unchallenged {P : Params} {s : St} {cl : CloseRec}
    (hr : Reachable P s) (hc : s.close = some cl) (hnf : cl.finalized = false)
    (hch : cl.challenged = false) :
    (s.finalizeSt cl).wBob = cl.bal ∧
    (s.finalizeSt cl).wAlice = s.deposit - cl.bal ∧
    cl.bal ≤ s.deposit := by
  have ⟨h1, h2, _⟩ := finalize_payout hr hc hnf
  have ⟨_, hbal⟩ := (reachable_inv hr).closeInv _ hc
  rw [hch] at h1 h2
  simp only [if_neg Bool.false_ne_true] at h1 h2
  exact ⟨h1, h2, hbal⟩

/-! ## Theorem 3 — terminality / at-most-once settlement -/

/-- The close is recorded and finalized. -/
def finalizedP (s : St) : Prop := ∃ cl : CloseRec, s.close = some cl ∧ cl.finalized = true

/-- The close is recorded and challenged. -/
def challengedP (s : St) : Prop := ∃ cl : CloseRec, s.close = some cl ∧ cl.challenged = true

/-- **Finality is absorbing.** No transition un-finalizes a close. In
particular `finalize` (guard `!finalized`), `timeoutForfeit` and every
`close*` (guard `mode == NONE`) can never fire again. -/
theorem finalized_absorbing {P : Params} {s s' : St} (st : Step P s s')
    (hf : finalizedP s) : finalizedP s' := by
  have ⟨cl, hc, hfin⟩ := hf
  cases st with
  | tick t ht => exact ⟨cl, hc, hfin⟩
  | openChannel D hD hcid => exact ⟨cl, hc, hfin⟩
  | requestClose hex hreq => exact ⟨cl, hc, hfin⟩
  | closeGenesis n1 hex hnone hpf => rw [hnone] at hc; cases hc
  | closeSigned nNext bal hex hnone hbal hpf => rw [hnone] at hc; cases hc
  | closeUnsigned cX nX nNext bal delta hex hnone hbal hpf => rw [hnone] at hc; cases hc
  | challenge nM cM hc' hnch hwin hpf hss hcol =>
      rw [hc'] at hc; cases hc
      exact ⟨_, rfl, hfin⟩
  | finalize hc' hnf hwin =>
      rw [hc'] at hc; cases hc
      rw [hfin] at hnf; cases hnf
  | timeoutForfeit hex hnone hdl => rw [hnone] at hc; cases hc
  | withdrawBob hw => exact ⟨cl, hc, hfin⟩
  | withdrawAlice hw => exact ⟨cl, hc, hfin⟩

/-- **A challenge is terminal.** No transition un-challenges a close (the
`challenge` guard `!challenged` also makes re-challenging impossible). -/
theorem challenged_absorbing {P : Params} {s s' : St} (st : Step P s s')
    (hch : challengedP s) : challengedP s' := by
  have ⟨cl, hc, hcl⟩ := hch
  cases st with
  | tick t ht => exact ⟨cl, hc, hcl⟩
  | openChannel D hD hcid => exact ⟨cl, hc, hcl⟩
  | requestClose hex hreq => exact ⟨cl, hc, hcl⟩
  | closeGenesis n1 hex hnone hpf => rw [hnone] at hc; cases hc
  | closeSigned nNext bal hex hnone hbal hpf => rw [hnone] at hc; cases hc
  | closeUnsigned cX nX nNext bal delta hex hnone hbal hpf => rw [hnone] at hc; cases hc
  | challenge nM cM hc' hnch hwin hpf hss hcol =>
      exact ⟨_, rfl, rfl⟩
  | finalize hc' hnf hwin =>
      rw [hc'] at hc; cases hc
      exact ⟨_, rfl, hcl⟩
  | timeoutForfeit hex hnone hdl => rw [hnone] at hc; cases hc
  | withdrawBob hw => exact ⟨cl, hc, hcl⟩
  | withdrawAlice hw => exact ⟨cl, hc, hcl⟩

/-- **Credits happen at most once.** After settlement, no transition changes
either party's total credit (pending + withdrawn): there is no double
finalize, no double timeout, and `withdraw` only moves value from pending to
paid. Together with `settlement_conservation` this pins each party's lifetime
take from the channel. -/
theorem settled_credits_frozen {P : Params} {s s' : St} (hr : Reachable P s)
    (st : Step P s s') (hf : finalizedP s) :
    s'.wBob + s'.pBob = s.wBob + s.pBob ∧
    s'.wAlice + s'.pAlice = s.wAlice + s.pAlice := by
  have ⟨cl, hc, hfin⟩ := hf
  have hinv := reachable_inv hr
  cases st with
  | tick t ht => exact ⟨rfl, rfl⟩
  | openChannel D hD hcid =>
      have := (hinv.virgin hcid).1
      rw [this] at hc; cases hc
  | requestClose hex hreq => exact ⟨rfl, rfl⟩
  | closeGenesis n1 hex hnone hpf => rw [hnone] at hc; cases hc
  | closeSigned nNext bal hex hnone hbal hpf => rw [hnone] at hc; cases hc
  | closeUnsigned cX nX nNext bal delta hex hnone hbal hpf => rw [hnone] at hc; cases hc
  | challenge nM cM hc' hnch hwin hpf hss hcol => exact ⟨rfl, rfl⟩
  | finalize hc' hnf hwin =>
      rw [hc'] at hc; cases hc
      rw [hfin] at hnf; cases hnf
  | timeoutForfeit hex hnone hdl => rw [hnone] at hc; cases hc
  | withdrawBob hw => exact ⟨by simp; omega, rfl⟩
  | withdrawAlice hw => exact ⟨rfl, by simp; omega⟩

/-- **No over-withdrawal.** Bob's lifetime take (pending + withdrawn) never
exceeds the deposit; symmetrically for Alice. -/
theorem withdrawn_bounded {P : Params} {s : St} (hr : Reachable P s) :
    s.wBob + s.pBob ≤ s.deposit ∧ s.wAlice + s.pAlice ≤ s.deposit := by
  have h := reachable_inv hr
  have hcred := h.credited
  have hle := settledAmt_le h
  cases hop : s.opened with
  | true => simp [hop] at hle; omega
  | false =>
      have hd := (h.virgin hop).2.2
      simp only [hop, if_neg Bool.false_ne_true] at hle
      omega

/-! ## Theorem 4 — liveness of the honest close -/

/-- **Unchallenged close settles the full deposit.** From any reachable state
with a pending, unchallenged, unfinalized close, letting the clock pass the
window and calling `finalize` (either party may) yields a reachable state in
which the whole deposit is creditable: Bob holds exactly `bal`, Alice exactly
`D − bal`, summing to `D`. This is the on-chain half of Spec §7's Alice
liveness: an honest close that draws no challenge always pays out in full. -/
theorem unchallenged_close_settles {P : Params} {s : St} {cl : CloseRec}
    (hr : Reachable P s) (hc : s.close = some cl)
    (hnc : cl.challenged = false) (hnf : cl.finalized = false) :
    ∃ s₁ s₂ : St, Step P s s₁ ∧ Step P s₁ s₂ ∧ Reachable P s₂ ∧
      s₂.wBob = cl.bal ∧ s₂.wAlice = s.deposit - cl.bal ∧
      s₂.wBob + s₂.wAlice = s.deposit := by
  have h := reachable_inv hr
  have hcred := h.credited
  have hbal := (h.closeInv _ hc).2
  simp only [settledAmt, hc, hnf, if_neg Bool.false_ne_true] at hcred
  -- advance the clock past the challenge window …
  let s₁ : St := { s with now := max s.now (cl.t0 + P.tau + 1) }
  have hst1 : Step P s s₁ := Step.tick _ (Nat.le_max_left _ _)
  have hc1 : s₁.close = some cl := hc
  have hwin : cl.t0 + P.tau < s₁.now := by
    show cl.t0 + P.tau < max s.now (cl.t0 + P.tau + 1)
    have := Nat.le_max_right s.now (cl.t0 + P.tau + 1)
    omega
  -- … then finalize.
  have hst2 : Step P s₁ (s₁.finalizeSt cl) := Step.finalize hc1 hnf hwin
  refine ⟨s₁, s₁.finalizeSt cl, hst1, hst2, (hr.step hst1).step hst2, ?_, ?_, ?_⟩
  · show s.wBob + payBob s.deposit cl = cl.bal
    simp [payBob, hnc]; omega
  · show s.wAlice + payAlice s.deposit cl = s.deposit - cl.bal
    simp [payAlice, payBob, hnc]; omega
  · show s.wBob + payBob s.deposit cl + (s.wAlice + payAlice s.deposit cl) = s.deposit
    simp [payAlice, payBob, hnc]; omega

end Zkpc.Confetti
