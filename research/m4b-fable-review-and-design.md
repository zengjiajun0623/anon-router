# M4b: adversarial review of M4a + on-chain contract design

Fable 5, 2026-07-19. Read-only pass over Spec-v2, `confetti/*.py`, `tests/test_confetti.py`,
`server.py`, and the phase-0 proving benchmark. Two jobs: (1) find correctness/safety bugs in
the M4a Python referee relative to Spec-v2; (2) design the M4b on-chain contract.

**M4a verdict: BUGS-FOUND (1 critical, 1 high, 3 medium/low).** The critical one is a total theft
of the deposit through `close_unsigned`, reproduced with a runnable PoC below. The `check_R_pay`
core (both parent branches, chain equation shape, `bal<=D`, output binding) and the
challenge/exhibit-set logic per mode are otherwise faithful and sound. The signed/genesis close
paths and the whole dedup->challenge->forfeit path are correct.

---

## JOB 1 — Review findings (most severe first)

### BUG 1 (CRITICAL, theft) — `close_unsigned` payout and exhibit set are unbound witness inputs

`confetti/channel.py:102-105`:

```python
def close_unsigned(self, cid, st, pi, bal_x, N_next, N_reveal, now):
    assert self.prover.verify(st, pi), "unsigned close proof invalid"
    self._record_close(cid, "unsigned", bal_x, {N_reveal, N_next}, st.C_i, now)
```

`prover.verify(st, pi)` only checks R_pay for the public statement `st = (delta, N_i, C_i, root)`.
R_pay's public inputs do **not** include the balance or the committed next-nullifier. `bal_x`,
`N_next`, and `N_reveal` are passed as separate arguments and are **never cross-checked** against
`st` or the witness. The settlement then pays out on `cr.bal = bal_x`
(`settle`, `channel.py:133`) and the challenge test fires only on `cr.exhibit = {N_reveal, N_next}`
(`challenge`, `channel.py:123`). Both are attacker-chosen.

**Exploit (verified, runnable):** Alice makes honest signed payments (Bob earns 500), then
submits any one of her genuine, valid R_pay proofs to `close_unsigned` while lying about the
side-channel values: `bal_x = 0`, `N_reveal = N_next = os.urandom(32)`. The proof verifies. The
recorded balance is 0 and the exhibit set is two random values that appear in none of Bob's held
messages, so Bob cannot challenge. Window elapses, Alice takes the whole deposit.

```
Bob earned 500 via honest payments
proof valid: True
Bob can challenge: False
settlement (to_bob, to_alice): (0, 1000)   # Bob robbed of all 500 he earned
```

Per Spec-v2 §4, the unsigned close "Publishes `C_x`, `reveal N_x`, `reveal N_{x+1}`, `bal_x`,
`delta_x`, with a proof: the full R_pay relation for `C_x`." The published `bal_x` and `N_{x+1}`
(and `N_x = st.N_i`) must be **bound by the close proof to `C_x`**, i.e. they are additional public
inputs the circuit constrains via `C_x = Com(cid, D, bal_x, N_{x+1}; r)` and `N_x = st.N_i`. The
M4a referee reuses the plain R_pay statement, whose public inputs expose none of these, and then
trusts the caller for all three. This is the single most important thing M4b must get right (see
the `R_closeUnsigned` relation in the design).

Note the same-state exception itself is fine: `cr.C_x = st.C_i` is the real closed commitment, and
`challenge` correctly rejects `m.C_i == cr.C_x`. The break is purely the unbound `bal_x` and
exhibit set. Fix (M4a): derive `N_reveal := st.N_i`, and require `bal_x`/`N_next` as
proof-bound public inputs (in the ClearWitnessProver, cross-check them against the witness's
`bal_i` and `null_next(st.N_i, c)` before recording).

### BUG 2 (HIGH, anonymity/fidelity) — no epoch-quantized roots; every historical root is accepted

`confetti/channel.py:71,75-79,85`:

```python
self._roots = {self.registry.root()}      # every root ever published
...
def accepts_root(self, r): return r in self._roots
```

Spec-v2 §3 makes root selection **normative** (the F-R1-3 privacy repair): roots are
epoch-quantized (epoch length `T_root`, default 1 day), proofs MUST cite the current epoch's root,
and verifiers accept **only the current and immediately previous epoch's** roots. The M4a referee
keeps and accepts the entire root history. Consequences:

- **Anonymity regression:** a payment that cites a rare old root fingerprints the client and bounds
  the channel's open date, which is exactly the leak §3 was written to close. The M4a referee
  re-opens it.
- **Not implementable on-chain as written:** unbounded root retention is infeasible; the accept
  rule has to be a 2-epoch window regardless.

M4a "works" for tests because they never advance epochs, but the epoch rule is unimplemented. M4b
must build it in (design below). Severity is high because it silently violates a normative,
already-gated privacy property.

### BUG 3 (MEDIUM, fidelity/soundness) — signed branch of `check_R_pay` omits the `c`-in-`C_open` binding

`confetti/relation.py:92-102` (signed branch) vs `:86` (genesis branch). Spec-v2 §3 constraint 2
(chain equation) is a top-level constraint over both branches: `N_{i+1} = H(N_i, c)` "with `c` the
value committed in `C_open`." The genesis branch enforces `open_commit(w.c, w.r_open) == w.C_open`
(`:86`); the **signed branch never does**. So in the signed branch `c` is a free witness used only
in `null_next(st.N_i, w.c)`, and any `c` is accepted.

**Verified:** `check_R_pay` returns `None` (accept) for a signed-branch payment whose `c` is a
fresh random value unrelated to `C_open`.

Exploitability: I could not turn this into theft or framing. The load-bearing safety link is
structural (a child's revealed `N_i` equals its parent's committed next-nullifier, enforced by the
`C_prev` opening check at `:97` and the parent's output binding), and that holds for any `c`. The
framing constructions a malicious Bob would attempt all require the parent commitment's blinding
`r` (hiding), which he does not know, so non-frameability survives. But the **implemented relation
is strictly weaker than the R_pay the Lean corpus targets** (the chain is no longer pinned to a
single committed `c`), so the machine-checked safety theorems do not actually cover this code path.
Any later change that publishes `r` or swaps in a non-hiding `Com` would convert this into a live
framing vector. Fix is one line: hoist the `open_commit(w.c, w.r_open) != w.C_open` check out of
the genesis branch so it guards both cases.

### BUG 4 (MEDIUM, operational, M4b-blocking) — Bob's safety-critical state is non-persistent

`server.py:86` instantiates one in-memory `Recipient`; `Recipient.seen` (global nullifier dedup),
`Recipient.inbox` (challenge evidence), and the XMSS signer index/keys (`wots.py`) live only in
process memory. On a server restart:

- `seen` is lost -> Bob will re-countersign a previously-seen nullifier, defeating the
  "unsigned frontier is at most one message deep" invariant and thus stale-close detection.
- `inbox` is lost -> Bob holds no evidence and cannot challenge a stale close -> theft.
- The `Xmss` tree is regenerated with a fresh `pk_B` -> every open channel (which named the old
  `pk_B`) is orphaned. (If instead the key were reloaded without its monotonic index, that is
  one-time-key reuse and catastrophic forgeability.)

The server comment acknowledges the reset as an M4a simplification, but for M4b this is
safety-critical: Bob's dedup set, inbox, signing key, and next-index must be durably persisted and
crash-consistent (Spec §9 persist-before-send is the Alice-side analogue).

### BUG 5 (LOW) — `Contract.open` does not enforce one record per `cid`

`channel.py:81-86` overwrites `self.channels[cid]` and appends a new registry leaf unconditionally.
`server.py:197` guards against a duplicate `cid`, but the referee itself does not, so two records
with the same `cid` and different `pk_B`/`D` can coexist in the tree. R_pay's GN-1 membership is
cid-matched, but cid-uniqueness is a registry invariant the contract must uphold. M4b must reject a
second `open` for an existing `cid`.

### Points that are correct (checked, not bugs)

- `check_R_pay` genesis branch: membership, `C_open` opens to `c`, `bal_prev == 0`, `N_i=H(cid,c)`,
  chain equation, `delta>=0`, `bal_i=bal_prev+delta`, `bal_i<=D`, output binding. Faithful.
- `close_signed` and `close_genesis`: correct openings, signature verification, `bal<=D`, correct
  mode-dependent exhibit sets (`{N_next}` and `{N_1}`). Faithful to §4.
- `challenge`: window check, root acceptance, proof validity, per-mode same-state exception
  (unsigned `C_m != C_x`; genesis every valid `m`; signed none), `N_m in E`. Faithful to §5. The
  window/settle boundary (`now > opened_at+tau`) has no gap or double-accept.
- Non-frameability of signed/genesis/unsigned closes holds because the challenge constructions
  require an unknown hiding-commitment blinding `r`.
- Dedup, fork inertness, overspend cap, XMSS forgery rejection: all correct, and the adversarial
  test suite exercises them.

---

## JOB 2 — M4b on-chain contract design

Target: Solidity on local Anvil (throwaway dev keys) first, then Base Sepolia. The deposit must
leave the operator's custody and live in the contract; settlement is enforced by contract code and
an on-chain proof verifier, replacing the trusted in-memory Python referee.

### Design axioms carried from the review

1. **On-chain proofs MUST be the ZK STARK (Groth16-wrapped), never the clear witness.**
   `ClearWitnessProver` serializes `cid, c, balances, parent, sigma` in the clear (`relation.py`).
   Putting that on-chain publishes the witness to every observer: it reveals the chain secret `c`,
   the balance, and the parent linkage, which is a total anonymity break. So the on-chain verifier
   consumes only public inputs plus an opaque proof. The local Anvil demo uses a **mock verifier
   that returns `true`** (clearly labeled, zero security, for wiring/gas-shape testing only); the
   real deployment uses the SP1 Groth16 verifier gateway. They are interchangeable behind
   `IVerifier`.
2. **Every value that drives payout or a challenge test is a proof-bound public input** (the BUG 1
   fix). Nothing that decides money is a bare calldata argument the caller can lie about.
3. **Root acceptance is a 2-epoch window** (the BUG 2 fix), enforced on-chain.

### Verifier interface

```solidity
interface IVerifier {
    // Mirrors ISP1Verifier.verifyProof: reverts on failure, returns on success.
    function verify(
        bytes32 vkey,            // per-relation program verifying key
        bytes   calldata publicInputs,
        bytes   calldata proof
    ) external view;
}
```

Four registered vkeys, one per relation: `VK_PAY` (R_pay), `VK_GENESIS`, `VK_CLOSE_SIGNED`,
`VK_CLOSE_UNSIGNED`. The contract ABI-decodes `publicInputs`, then cross-checks each field against
on-chain truth (the channel's `D`, `pkB`, an accepted `root`, the published `bal`/`N`) before
trusting the proof. `MockVerifier.verify(...) { }` (empty body = always succeeds) is the demo
stand-in; `SP1Verifier` forwards to the deployed SP1 gateway. Swapping the constructor arg is the
only change between demo and real.

### Storage

```solidity
enum Status { None, Open, Closing, Settled }
enum Mode   { Genesis, Signed, Unsigned }

struct Channel {
    address depositor;   // gets the refund; the only party who can be paid Alice-side
    uint256 D;           // deposit, held by this contract
    bytes32 pkB;         // recipient (router) XMSS root; public
    bytes32 cOpen;       // Com(c; r_open)
    uint64  openedAt;
    uint64  closeReqAt;  // Bob's requestClose timestamp (0 = none)
    Status  status;
}

struct Close {
    Mode    mode;
    uint256 balClaim;    // proof-bound; payout basis
    bytes32 cx;          // published only for Unsigned (same-state exception)
    bytes32 e0;          // exhibit nullifier 1  (always set)
    bytes32 e1;          // exhibit nullifier 2  (Unsigned only; else 0)
    uint64  startedAt;   // challenge window opens here
    bool    challenged;
    bool    finalized;
}

mapping(bytes32 => Channel) public channels;   // cid => Channel
mapping(bytes32 => Close)   public closes;      // cid => Close

// Registry: append-only incremental Merkle tree over channel-record leaves.
// Keep only the frontier on-chain; snapshot the root per epoch.
bytes32[REG_HEIGHT] internal frontier;
uint256 internal leafCount;
mapping(uint256 => bytes32) public epochRoot;   // epoch => root snapshot
uint256 public constant T_ROOT = 1 days;        // epoch length (Spec §3 default)
```

### Registry root maintenance (Spec §3, epoch-quantized)

- The leaf is `H("chrec", cid, D, pkB, cOpen)`, identical to `chain.py:ChannelRecord.leaf()`.
- `open` appends the leaf via an incremental Merkle insert (O(REG_HEIGHT) hashes, storing only the
  frontier), then writes `epochRoot[block.timestamp / T_ROOT] = currentRoot()`. So the mapping
  always holds the latest root for the current epoch; earlier epochs keep their last snapshot.
- **Accept rule:** `rootAccepted(r)` is true iff `r == epochRoot[e]` or `r == epochRoot[e-1]` for
  `e = block.timestamp / T_ROOT`. Nothing older. This is the on-chain form of the normative §3
  window and directly fixes BUG 2.
- Payment/close/challenge proofs carry `root` as a public input; the contract checks
  `rootAccepted(root)` before honoring the proof. Alice reads the current `epochRoot` (and her
  membership path, rebuilt from `RecordInserted` events) to build proofs; because the root is a
  public clock value common to all channels, it links nothing.
- `just-closed-channel service loss` (§7) is the disclosed, bounded residual: a payment citing the
  previous epoch's root may reference a channel that closed up to ~2 epochs ago. Bob accepts the
  service-value risk per §7; the mitigation lever (accept only current epoch at close) stays a
  parameter.

### External functions

| Function | Signature (public inputs in the proof shown in braces) | What it verifies / does |
|---|---|---|
| `open` | `open(bytes32 cid, bytes32 cOpen) payable` | `status==None`, `msg.value==D` (or ERC20 `transferFrom`), `pkB` = the operator's registered router key. Records the `Channel`, inserts the leaf, snapshots the epoch root, emits `Opened(cid, D, leafIndex)`. Deposit now in contract custody. Enforces cid-uniqueness (BUG 5 fix). |
| `requestClose` | `requestClose(bytes32 cid)` | Callable by the recipient/operator. Sets `closeReqAt`, starting the `T_req` clock. |
| `closeGenesis` | `closeGenesis(bytes32 cid, bytes32 N1, bytes proof)` {`cid, cOpen, N1`} | `VK_GENESIS`: proves `cOpen` opens to some `c` with `N1 = H(cid, c)`. `balClaim=0`, `E={N1}`. `c` stays a private witness (not revealed on-chain). Opens the window. |
| `closeSigned` | `closeSigned(bytes32 cid, uint256 balX, bytes32 Nnext, bytes proof)` {`cid, D, pkB, balX, Nnext`} | `VK_CLOSE_SIGNED`: proves knowledge of `C, r, sigma` with `C=Com(cid,D,balX,Nnext;r)`, `Verify(pkB,C,sigma)`, `balX<=D`. Contract binds `D,pkB` to the channel record. No `C_x` published (F-R2-1). `balClaim=balX`, `E={Nnext}`. |
| `closeUnsigned` | `closeUnsigned(bytes32 cid, uint256 balX, uint256 deltaX, bytes32 Nx, bytes32 Nnext, bytes32 Cx, bytes32 root, bytes proof)` {`cid, D, root, deltaX, Nx, Cx, balX, Nnext`} | `VK_CLOSE_UNSIGNED`: the full R_pay relation for `Cx` **with `balX`, `Nnext`, `Nx` as bound public inputs** — the circuit constrains `Cx=Com(cid,D,balX,Nnext;r)`, `Nx` is the revealed parent nullifier, `deltaX>=0`, `balX<=D`, against an accepted `root`. Contract checks `rootAccepted(root)`. `balClaim=balX`, `cx=Cx`, `E={Nx,Nnext}`. This is the BUG 1 fix in relation form. |
| `challenge` | `challenge(bytes32 cid, uint256 deltaM, bytes32 Nm, bytes32 Cm, bytes32 root, bytes proof)` {`deltaM, Nm, Cm, root`} | Within window. `VK_PAY` verifies R_pay for `m`; `rootAccepted(root)`; per-mode same-state exception (`Unsigned`: `Cm != cx`; `Genesis`: none; `Signed`: none); `Nm in E`. On success sets `challenged=true`. Forfeits `D` to the recipient at finalize. |
| `finalize` | `finalize(bytes32 cid)` | After `startedAt + tau`. Either party may call (Spec §4). If `challenged`: pay `D` to recipient. Else pay `balClaim` to recipient, `D-balClaim` to `depositor`. Marks `Settled`, pull-payment credited. |
| `timeoutForfeit` | `timeoutForfeit(bytes32 cid)` | Recipient-callable. If `status==Open` and (`now > openedAt + T_abs`) or (`closeReqAt!=0 && now > closeReqAt + T_req`), and no close pending: pay `D` to recipient, mark `Settled`. |

Time constants match Spec §1: `T_abs=90d`, `T_req=7d`, `tau=7d`, `T_ROOT=1d`.

### Trust property each on-chain function delivers vs the M4a in-memory referee

- **M4a:** the `Contract` object is a trusted local Python referee; "custody" is notional (the
  deposit is a Python int); a Byzantine Alice who calls the methods directly can drive BUG 1. Safety
  rests on callers being honest about side-channel arguments.
- **M4b:** the deposit is real value escrowed in the contract, released only by `finalize`/
  `timeoutForfeit` per code. Every payout-deciding value is a proof-bound public input checked
  against on-chain channel state, so no caller-supplied argument can misdirect funds (BUG 1 closed).
  Root acceptance is the 2-epoch window (BUG 2 closed). The challenge window is on-chain block time,
  not a passed `now`. The verifier is ZK, so the on-chain transcript leaks only what §8 already
  concedes (cid, D, split, and the challenge tuple on a cheating close), never the witness.

### Migration path (server.py / channel.py stay consistent with the chain)

1. **Contract abstraction.** Replace the in-memory `Contract` with a thin `ChainContract` adapter
   exposing the same method names (`open`, `close_*`, `challenge`, `settle`, `root`,
   `accepts_root`) but backed by web3 calls to the deployed contract. `Payer.open_on` becomes an
   `open` transaction that actually moves USDC/ETH out of Alice's wallet; `register` reads the
   returned `leafIndex` and rebuilds the membership path from `Opened` events.
2. **Bob becomes an on-chain watcher.** `Recipient` gains a persistent store (sqlite: `seen`,
   `inbox` keyed by `N_i`, XMSS index) fixing BUG 4. A watcher subscribes to `Closed(cid, mode, E0,
   E1, Cx)` events. Because Bob cannot attribute a cid to an inbox message (by design), on every
   `Closed` event he scans his inbox for any message `m` with `m.N_i in {E0, E1}` and
   `m.C_i != Cx`; if found he submits `challenge(...)` before `startedAt + tau`. This is exactly
   the M4a `challenge` predicate, moved to an event-driven loop.
3. **Countersign flow unchanged.** Off-chain payment/countersign over HTTP
   (`server.py:/v1/chat/completions` X-Channel-Payment / X-Channel-Countersign) is untouched; only
   dedup and inbox persistence change. `bob.accept` still verifies the STARK proof natively
   off-chain (phase-0: ~0.06s verify), which is the per-request path, so no on-chain cost per
   payment.
4. **Finalize/refund.** Alice's client calls `closeSigned`/`closeUnsigned`/`closeGenesis` and, after
   the window, `finalize`; Bob's watcher also calls `finalize` so a passive Alice cannot stall his
   payout (§4 either-party finalize).

### Sharpest risks

- **Reentrancy.** `finalize`/`timeoutForfeit` move funds. Use checks-effects-interactions plus a
  pull-payment ledger (`withdrawable[addr] += amount`; a separate `withdraw()` does the transfer)
  and a `nonReentrant` guard. Never `.call` a payout before flipping `status` to `Settled`.
- **Challenge-window griefing / liveness.** Bob must observe every `Closed` event and get a
  challenge mined within `tau`. If he is offline for the whole window he loses the evidence race and
  Alice's stale close settles. `tau=7d` gives ample margin, but the watcher needs redundancy
  (multiple nodes, keeper backup) and monitored liveness. Conversely Alice cannot grief Bob: a
  passive Alice is handled by either-party `finalize` and by `timeoutForfeit`.
- **Root staleness / epoch handling.** The 2-epoch accept window is the crux. Set `T_ROOT`
  deliberately: too short narrows the anonymity set and risks proofs going stale mid-flight (a proof
  built against `epochRoot[e]` must land while `e` or `e+1` is current); too long widens the
  just-closed-channel service-loss window (§7). Snapshot the root on every `open` and also lazily on
  first touch of a new epoch so `epochRoot[e-1]` is always populated. Emit `RecordInserted` so
  clients can rebuild membership paths without an archive node.
- **Gas of on-chain proof verification.** Groth16 verify is ~270k gas per proof (phase-0
  reference). Only `close*` and `challenge` carry proofs (payments are off-chain), so the on-chain
  cost is a few hundred k gas per channel lifecycle, once. `open` carries no proof; its cost is the
  incremental Merkle insert (REG_HEIGHT hashes). All comfortably fine on Base. The SP1 Groth16
  wrapping (host side, ~tens of seconds to ~2 min, unmeasured on the M4-mac, needs Docker/Linux) is
  a client-side latency at close, not a gas cost.
- **Bob's challenge trigger and funding.** The challenge is event-driven and operator-funded: Bob
  (the router operator) pays the challenge gas out of his own hot wallet. That is aligned incentive
  (he is defending his own earnings), but the wallet must stay funded and the watcher must not miss
  events; budget a keeper with a gas alarm. A stuck or underfunded challenger inside `tau` is the
  primary way Bob loses money in M4b, so it is the operational SLO to watch.
- **Verifier trust and vkey pinning.** The four vkeys must be immutable post-deploy (or governed
  behind a timelock); a swappable verifier or mutable vkey is a rug on every open channel. The mock
  verifier must be impossible to select on any non-dev deployment (compile-time flag or separate
  contract, never a constructor arg on mainnet/testnet).
