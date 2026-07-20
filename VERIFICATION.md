# Verification status — anon-router / confetti

How each layer is checked, and where the trust boundaries are.

## Off-chain protocol (`confetti/`)

- 14 adversarial Python tests (`tests/test_confetti.py`): happy path,
  stale-close→challenge→forfeit, honest-close inertness, dedup/fork, overspend
  cap, forgery, liveness, unsigned-close balance binding.

## On-chain contract (`contracts/src/ConfettiChannels.sol`)

- 15 Foundry tests: escrow, three close modes, challenge→forfeit, timeout,
  cid-reuse, epoch-root stability, pull-payment.
- **Three independent code reviews + one machine-checked proof, converging.**
  Every fund-loss bug found was fixed and regression-tested:
  - **Fable 5** — CRITICAL unsigned-close payout unbound to proof (theft);
    + epoch-root anonymity, chain-secret binding, persistence, cid-uniqueness.
  - **Codex** — CRITICAL intra-epoch root front-run; HIGH cid-reuse fund-lock;
    MEDIUM push-payment griefing.
  - **Kimi K2** (via `kimi` CLI) — 27-candidate pass, no exploitable bug in the
    fixed contract; independently flagged the same `timeoutForfeit` edge the
    Lean proof found, confirmed inert. Raised verifier-reentrancy — a non-issue
    because `IVerifier.verify*` is `view` (staticcall, cannot reenter).
  - Reviews saved in `research/` (`m4b-fable-review-and-design.md`,
    `kimi-review-confetti.md`).

## Machine-checked safety proof (`lean/ConfettiContract.lean`)

Lean 4 model of the contract's state machine (all 11 transitions,
require-by-require), with `IVerifier` as an **arbitrary predicate** — so safety
holds for any verifier behavior. Zero `sorry`; `#print axioms` rests only on
`propext / Classical.choice / Quot.sound`. Cold `lake build` green in <1s.

Proven: conservation, solvency, settlement-conservation; no-theft
(challenged→Bob=D/Alice=0, unchallenged→Bob=bal/Alice=D−bal, bal≤D);
terminality (finalized/challenged absorbing, settled credits frozen, take
bounded by D); liveness (unchallenged close settles). Found and proved-inert
the `timeoutForfeit` openedAt=0 edge (the same one Kimi flagged).

## Contract-in-Lean (`verity/Contracts/ConfettiSettle/`)

Settlement+challenge core written in the **Verity** Lean-4 EDSL that compiles to
EVM bytecode, proving conservation + no-theft on the artifact that deploys.
(In progress; see `research/verity-confetti.md`.) Merkle registry and ZK
verification are documented trust boundaries.

## Real ZK verifier (M4b-real, `research/m4b-groth16/`)

SP1 guest proving the genesis branch of R_pay with full Merkle membership and
byte-exact hashing; `SP1PayVerifier.sol` staged; Docker up via colima+Rosetta.
Remaining: signed-branch XMSS disjunct + final Groth16 wrap. Until then the
on-chain demo uses MockVerifier (never on a real network).

## End-to-end (`run_e2e.sh`)

7/7 stages on local Anvil: mint key, on-chain deposit, watcher credit, real
inference, metered debit, channel honest close, channel fraud forfeit.

## Trust boundaries (explicit)

- The ZK verifier (proof soundness) — but on-chain safety is proven independent
  of it (Lean: arbitrary verifier predicate).
- The Merkle registry membership (abstracted in the Lean/Verity models).
- The router's off-chain state (dedup/inbox/XMSS) — needs durable persistence
  before mainnet.
- MockVerifier is demo-only; deploy script refuses it on a real network.
