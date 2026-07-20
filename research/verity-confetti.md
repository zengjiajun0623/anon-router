# Verity (Lean→EVM) bytecode-level path for the confetti settlement core

Status: **complete** · 2026-07-20
Scope gate: this document is the precise record of what the Verity `ConfettiSettle`
contract proves, what bytecode it compiles to, and what remains trusted. It gates a
real-money contract; read the trust-boundary section before relying on any claim.

---

## 1. What was built

`ConfettiSettle` is the on-chain settlement state machine of
`contracts/src/ConfettiChannels.sol` (Spec-v2 §4–§7), re-implemented inside the
vendored Verity Lean-4 EDSL (`verity/`), compiled to EVM bytecode through
Verity's proven compilation pipeline, and covered by 47 machine-checked
theorems with **zero project axioms and zero `sorry`**.

Files (all under `verity/`):

| File | Content |
|---|---|
| `Contracts/ConfettiSettle/ConfettiSettle.lean` | the EDSL contract (12 entrypoints, 11 storage fields) |
| `Contracts/ConfettiSettle/Spec.lean` | storage accessors, `FinalizePre`/`TimeoutPre`, post-condition predicates |
| `Contracts/ConfettiSettle/Invariants.lean` | `ChannelWF` (boolean flags, claim ≤ deposit), `DistinctRoles` |
| `Contracts/ConfettiSettle/Proofs/Basic.lean` | views + `withdraw` (7 theorems) |
| `Contracts/ConfettiSettle/Proofs/Conservation.lean` | settlement credits, conservation, timeout forfeiture (10 theorems) |
| `Contracts/ConfettiSettle/Proofs/Correctness.lean` | open/close/challenge specs, at-most-once terminality, composed terminality (30 theorems) |
| `Contracts/ConfettiSettle/SpecProofs.lean` | layer-2 re-export (repo convention) |

Registered in `Contracts.lean`, `Contracts/Specs.lean` (`allSpecs`),
`packages/verity-examples/contracts.manifest`, `PrintAxioms.lean` (regenerated),
and the repo audit artifacts (`artifacts/storage_layout_report.json`,
`artifacts/STORAGE_LAYOUT_SUMMARY.md`, `artifacts/verification_status.json`,
`docs/VERIFICATION_STATUS.md`, `llms.txt`).

### Entrypoints and per-cid state machine

- `openChannel(cid, alice, bob)` **payable** — escrows `msg.value`, records
  payout roles and the open timestamp. Guarded by `mode[cid]==0 && deposit[cid]==0`
  (cid single-use forever, the `usedCid` invariant).
- `closeGenesis(cid)` / `closeSigned(cid, bal)` / `closeUnsigned(cid, bal)` —
  Alice-only; posts a close with `mode := 1/2/3`, claim `bal ≤ deposit`
  (genesis claims 0), challenge window starts at `block.timestamp`.
- `requestClose(cid)` — Bob-only, once; starts the `T_req` clock.
- `challenge(cid, valid)` — Bob-only, in-window, once, pre-finality; the whole
  evidence judgment is the abstract input `valid` (trust boundary, §5.1).
- `finalize(cid)` — anyone, after the window, once. Credits the pull-payment
  ledger: challenged ⇒ (D, 0) to (Bob, Alice); unchallenged ⇒ (bal, D−bal).
- `timeoutForfeit(cid)` — Bob-only, only while no close exists, after
  `chOpenedAt + tAbs` or `reqCloseAt + tReq`; forfeits D to Bob and marks the
  cid terminal (`mode := 2`, `finalizedFlag := 1`).
- `withdraw()` — pull-payment: zeroes the caller's ledger slot (ETH transfer is
  a trust boundary, §5.3).
- Views: `getWithdrawable(addr)`, `getDeposit(cid)`, `getMode(cid)`.

Timer literals (Solidity takes them as constructor immutables; values are the
`contracts/script/Deploy.s.sol` defaults): `tau = 604800` (7 d),
`tAbs = 7776000` (90 d), `tReq = 604800` (7 d).

### Storage layout (verified, emitted by the compiler)

All fields are Solidity-compatible keccak mappings (`keccak256(key ‖ slot)`),
one slot family each, no packing, no aliases — see
`artifacts/confetti_layout.json` and `artifacts/STORAGE_LAYOUT_SUMMARY.md § ConfettiSettle`:

```
slot 0 deposit[cid]      slot 4 finalizedFlag[cid]   slot 8  withdrawable[addr]
slot 1 mode[cid]         slot 5 openedAt[cid]        slot 9  chOpenedAt[cid]
slot 2 balClaim[cid]     slot 6 aliceOf[cid]         slot 10 reqCloseAt[cid]
slot 3 challenged[cid]   slot 7 bobOf[cid]
```

---

## 2. Build, axiom-audit, and bytecode evidence

### Build

```
$ cd verity && ~/.elan/bin/lake build          # default target (Verity lib)
Build completed successfully (2125 jobs).
$ ~/.elan/bin/lake build Contracts             # includes ConfettiSettle + all proofs
Build completed successfully.
$ make check                                   # full local CI-equivalent suite
All checks passed.
```

No `sorry`, no `admit`, no new `axiom` anywhere in the ConfettiSettle sources
(`grep` clean; `make check` includes the repo's sorry/axiom-location linters).

### Axiom audit (zero project axioms)

Ran both the repo-wide audit (`PrintAxioms.lean`, regenerated to include all 47
ConfettiSettle theorems, `lake env lean PrintAxioms.lean` → exit 0) and a
targeted audit using the same memoized transitive-constant scanner plus
independent `#print axioms` per theorem. Result for **every** ConfettiSettle
theorem:

```
depends on axioms: [propext] | [propext, Quot.sound] | [propext, Classical.choice, Quot.sound]
```

i.e. only Lean's three foundational axioms — the same footprint as core
Lean/Mathlib itself. Zero Verity/project-level axioms (consistent with
`AXIOMS.md`: "Verity has zero project-level Lean axioms"), zero `sorryAx`,
and — unlike some compiler-native proofs elsewhere in the repo — not even
`Lean.ofReduceBool`.

### Compiled EVM bytecode

```
$ ./.lake/build/bin/verity-compiler --module Contracts.ConfettiSettle.ConfettiSettle \
    -o artifacts/yul --abi-output artifacts/abi \
    --layout-report artifacts/confetti_layout.json --trust-report artifacts/confetti_trust.json
$ solc-0.8.33 --strict-assembly --optimize --bin artifacts/yul/ConfettiSettle.yul
```

| Artifact | Path | Fingerprint |
|---|---|---|
| Yul (verified pipeline output) | `verity/artifacts/yul/ConfettiSettle.yul` | sha256 `cbedc028…fbe95f` |
| Deployment bytecode (3213 bytes) | `verity/artifacts/ConfettiSettle.bin` | sha256 `28300220…059ff8` |
| ABI | `verity/artifacts/abi/ConfettiSettle.abi.json` | `openChannel` payable, rest nonpayable |
| Storage-layout report | `verity/artifacts/confetti_layout.json` | slots as table above |
| Trust-surface report | `verity/artifacts/confetti_trust.json` | boundary class `gate` only |

solc 0.8.33+commit.64118f21 (the version pinned by `verity/foundry.toml`).
One real bug was caught by inspecting the emitted dispatcher: the first cut of
`openChannel` compiled **non-payable** (dispatcher `if callvalue() revert`
before a `msg.value > 0` guard — unsatisfiable on-chain). Fixed with the EDSL's
`function payable` marker; the Lean semantics (`msgValue`) and all proofs are
unchanged.

### On-chain execution check (anvil, real bytecode)

The compiled bytecode was deployed on anvil and driven through all three
settlement paths, including every negative guard. All observations match the
proven theorems exactly:

- unchallenged: open 1 ETH → `closeSigned(bal=0.4e18)` → warp 8 d → `finalize`
  ⇒ `withdrawable[bob] = 0.4e18`, `withdrawable[alice] = 0.6e18`; `withdraw()`
  zeroes Bob's slot.
- challenged: open 2 ETH → close → `challenge(valid=0)` reverts
  `invalid challenge evidence` → `challenge(valid=1)` ok → re-challenge reverts →
  finalize ⇒ Bob +2 ETH, Alice's slot untouched.
- timeout: open 0.5 ETH → early `timeoutForfeit` reverts `no deadline passed` →
  `requestClose` (repeat reverts) → warp past `tReq` → `timeoutForfeit`
  ⇒ Bob +0.5 ETH, `mode = 2`; second timeout reverts `close pending`,
  `finalize` reverts `already finalized`.
- guards: cid reuse, non-alice close, double close, in-window finalize,
  re-finalize, challenge-after-finalize — all revert with the proven messages.

---

## 3. What is proven (47 theorems) and how it maps

Reference model: `lean/ConfettiContract.lean` (16-theorem transition-system
proof, reachability-based). The Verity layer proves *function-level* Hoare-style
specs about the deployable contract; the model proves *global reachability*
invariants. The two meet as follows.

### Settlement accounting (`Proofs/Conservation.lean`)

| Verity theorem | Statement | Model counterpart |
|---|---|---|
| `finalize_challenged_credits` | successful finalize with `challenged=1` sets `withdrawable[bob] += deposit`, leaves Alice's slot unchanged, sets `finalizedFlag=1` | `finalize_payout_challenged` (no-theft, challenged) |
| `finalize_unchallenged_credits` | with `challenged=0`: Bob `+= balClaim`, Alice `+= deposit − balClaim`, flag set | `finalize_payout_unchallenged` (no-theft, unchallenged) |
| `finalize_conservation` | Bob-credit + Alice-credit `= deposit` exactly (Nat-level, under `FinalizePre`, `ChannelWF`, distinct roles, no-overflow) | `conservation` / `settlement_conservation` / `finalize_payout` |
| `finalize_succeeds` | under the same guards finalize does not revert | liveness half of `unchallenged_close_settles` |
| `timeout_meets_spec` | successful timeout: `finalizedFlag=1`, `mode=2`, Bob `+= deposit` | `timeoutForfeit` transition + `credited` invariant |
| `timeout_preserves_others` | timeout touches no ledger slot but Bob's | ledger-isolation part of `Inv` |
| `timeout_conservation` | timeout credit delta `= deposit` exactly | `credited` (settledAmt = D) |

### State machine & terminality (`Proofs/Correctness.lean`)

| Verity theorem(s) | Property | Model counterpart |
|---|---|---|
| `openChannel_meets_spec` / `_reverts_pending` / `_reverts_used` / `_reverts_zero_deposit` | deposit escrow recorded exactly once per cid; cid single-use forever | `openChannel` guard `usedCid` |
| `closeSigned/Genesis/Unsigned_meets_spec`, `closeSigned_establishes_claim_bound` | close posts `mode∈{1,2,3}`, claim `≤ deposit`, window timestamp; deposit & ledger untouched | `closeInv` (claim bounded) |
| `close*_reverts_pending`, `closeSigned_reverts_overclaim`, `closeSigned_reverts_wrong_sender` | at most one close per cid; `bal > D` unpostable; Alice-only | close guards |
| `requestClose_meets_spec` / `_reverts_repeat` | `T_req` clock starts once, touches nothing else | `requestClose` |
| `challenge_meets_spec` | challenge sets exactly the flag: mode, deposit, claim, finality, **entire ledger** unchanged | `challenge` transition |
| `challenge_reverts_finalized` / `_rechallenge` / `_window_closed` / `_invalid_evidence` | challenge is once-only, in-window, pre-finality, evidence-gated | `challenged_absorbing` + guards |
| `finalize_reverts_refinalized` / `_no_close` / `_window_open` | finalize fires once, only on a posted close, only after the window; revert = state bit-for-bit unchanged (`Contract.run` rollback) | `finalized_absorbing`, `settled_credits_frozen` |
| `timeout_reverts_close_pending` | timeout never fires once any close exists (kills timeout-after-close, timeout-after-finalize, double-timeout) | `timeoutForfeit` guard `close = none` |
| `finalize_preserves_channel_challenged` | finalize writes only flag + ledger; deposit/mode/claim survive | frame condition |
| `timeout_then_finalize_reverts`, `timeout_then_timeout_reverts`, `finalize_then_finalize_reverts_challenged`, `challenge_then_challenge_reverts` | **composed terminality through actual post-states**: after a successful settlement/challenge, the forbidden second transition provably reverts | `settled_credits_frozen`, absorbing lemmas |

### Pull-payment (`Proofs/Basic.lean`)

`withdraw_meets_spec` (zeroes exactly the caller's slot), `withdraw_reverts_empty`,
`withdraw_preserves_channels` (never touches per-channel bookkeeping), plus
exact-read view specs — model counterpart: `withdrawBob/withdrawAlice`
transitions and `withdrawn_bounded` (the caller can never extract more than was
credited, because the slot is zeroed on first withdraw and only settlement — 
proven to happen at most once and to credit exactly `D` in total — ever adds).

### What stays at the model layer (deliberately)

The *global* inductive invariant over arbitrary interleavings — `conservation`
(`ethBal + paid == D`), `solvency` (`wBob + wAlice ≤ ethBal`),
`withdrawn_bounded` as a reachability statement — is proven once, over the
transition system, in `lean/ConfettiContract.lean`. The Verity theorems are the
per-transition facts (exact credits, frames, at-most-once guards) that make
each Solidity-level call an instance of a model transition. The Verity layer
does not re-prove the reachability induction; it proves that the deployable
code implements each transition exactly.

Overflow side conditions (`withdrawable + deposit ≤ MAX_UINT256` etc.) appear
as hypotheses; on-chain they are discharged by total ETH supply « 2^128, and
the compiled code additionally *checks* every add/sub (reverts on violation —
reverts are safe by the rollback theorems).

---

## 4. Correspondence to `ConfettiChannels.sol`

The proven fragment covers, guard-for-guard and in source order, the Solidity
functions `open` (escrow bookkeeping), `requestClose`, `closeGenesis`,
`closeSigned`, `closeUnsigned` (mode/claim/window bookkeeping), `challenge`
(flag + once/window/evidence guards), `finalize` (lines 251–265: the exact
`toBob = challenged ? D : bal; toAlice = D − toBob` split), `timeoutForfeit`
(lines 268–283 incl. the terminal `mode := SIGNED; finalized := true`
marking), `_credit`, and the ledger half of `withdraw`.

Known, intentional divergences (each is a strengthening or a bookkeeping
re-encoding, none weakens a Solidity guarantee):

1. **`challenge` requires `finalizedFlag == 0`.** Solidity relies on the window
   check; the model file documents a (financially inert) quirk where a
   post-timeout challenge could still flip the flag on a young chain. In
   ConfettiSettle the quirk is closed: `challenge_reverts_finalized` holds
   unconditionally on timestamps.
2. **`exists` flag elided.** Solidity's `ch.exists` is encoded as
   `deposit > 0 ∧ mode == 0` for timeout eligibility; `finalize` does not
   clear a flag (terminality is carried by `finalizedFlag`/`mode`, proven
   absorbing). Consequence: `requestClose` remains callable after settlement
   (inert — timeout is dead once `mode ≠ 0`).
3. **`cid : uint256`** instead of `bytes16`; role addresses stored as words.
4. **Timers inlined** as the `Deploy.s.sol` defaults instead of constructor
   immutables (constructor params with immutables are outside the fragment
   used here).
5. **No events.** Event emission was left out to stay in the smallest
   fully-verified fragment (Verity supports it; nothing financial depends on it).
6. **No reentrancy guard needed**: the compiled contract makes no external
   calls at all (see §5.3), so the Solidity `nonReentrant` has nothing to
   protect in this build.

This is a **re-implementation with proofs, not a bytecode-equivalence proof
of the deployed `ConfettiChannels.sol`**. The Solidity contract remains covered
by its model proof (`lean/ConfettiContract.lean`) only.

---

## 5. Explicit trust boundaries (what is NOT proven)

1. **ZK verifier (`IVerifier` / Groth16 / SP1).** Entirely outside. Closes take
   no proof arguments here; `challenge` takes the abstract verdict `valid`.
   Every theorem holds for *every* verifier behaviour, including a broken one
   that accepts garbage: the settlement layer's safety does not rest on proof
   soundness. What verifier soundness buys — that a *valid* challenge exists
   exactly when Alice posted a stale/fraudulent close — is the
   zk-payments-confetti evidence-layer proof (Spec-v2 §7), a separate campaign.
2. **Merkle registry & epoch roots.** `_insert` (unbounded loop over
   TREE_DEPTH), `_hashPair` keccak chains, `rootAccepted`, `_snapshotEpoch` are
   not modeled: they gate whether a proof is accepted (part of boundary 1) and
   move no funds. The write-once epoch-root property (F-R1-3 privacy repair) is
   *not* verified anywhere in Lean today.
3. **ETH transfer in `withdraw`.** External calls are outside Verity's verified
   fragment. The compiled ConfettiSettle `withdraw` zeroes the ledger slot and
   stops — **it does not send ETH**. The settlement-accounting core is proven;
   the `call{value: amount}` + success-check of `ConfettiChannels.sol:297` is
   trusted (and untestable here). Anyone deploying this bytecode as-is would
   strand funds; it is a verified core, not a complete payment channel.
4. **Yul → bytecode: solc 0.8.33 (pinned)** is trusted, per Verity's standard
   trust model (`TRUST_ASSUMPTIONS.md` §1). Lean → CompilationModel → IR → Yul
   is the proven pipeline (layers 1–3), with keccak mapping-slot derivation
   carrying the standard collision-resistance assumption.
5. **Checked-arithmetic local obligations.** The trust report
   (`confetti_trust.json`) lists the 7 emitted checked ops (window deadlines,
   ledger credits, refund subtraction) as `assumed` no-revert obligations:
   nothing proves they *don't* revert on-chain in extreme states — but every
   revert is proven state-preserving, so these are liveness, not safety, gaps
   (and `finalize_succeeds` discharges the finalize path under the stated
   no-overflow hypotheses).
6. **Timer values** are proven-about only for the default deployment constants;
   a deployment wanting different immutables must re-instantiate.
7. **Foundry property/differential harnesses** for ConfettiSettle were not
   written (contract is excluded with a comment in
   `scripts/check_contract_structure.py`, all 47 theorems marked proof-only in
   `test/property_exclusions.json`); behavioral evidence is the anvil
   end-to-end run in §2 instead. Porting the repo's 10k-transaction
   differential harness is the natural next hardening step.

## 6. Repro commands

```bash
cd ~/cleavelabs/anon-router/verity
~/.elan/bin/lake build && ~/.elan/bin/lake build Contracts       # proofs
~/.elan/bin/lake env lean PrintAxioms.lean                       # axiom audit (0 project axioms)
make check                                                       # full local CI suite
~/.elan/bin/lake build verity-compiler
./.lake/build/bin/verity-compiler --module Contracts.ConfettiSettle.ConfettiSettle \
  -o artifacts/yul --abi-output artifacts/abi \
  --layout-report artifacts/confetti_layout.json --trust-report artifacts/confetti_trust.json
"$HOME/Library/Application Support/svm/0.8.33/solc-0.8.33" \
  --strict-assembly --optimize --bin artifacts/yul/ConfettiSettle.yul
```

Proof-engineering note for future edits: the timer literals must be
*opacified* before whole-body reduction `simp`s (see the note at the top of
`Proofs/Conservation.lean`) — the Lean kernel unarily unfolds
`Nat.add symbolic bigLiteral` during match-scrutinee whnf and overflows its
recursion depth for values ≥ ~10^5. `tau_opaque` / `tAbs_opaque` are the
reusable surrogates.
