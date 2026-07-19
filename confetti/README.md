# confetti — trust-minimized payment channels for anon-router (M4a)

Reference implementation of the confetti nullifier-chain payment channel
(`~/cleavelabs/zk-payments-confetti/Spec-v2.md`), wired into the router as an
opt-in payment lane alongside vouchers and the free local lane.

## What M4a delivers

The whole payment protocol, running off-chain against an in-memory referee:

- **Nullifier chain + hidden balances** (`chain.py`): `N_{j+1}=H(N_j,c)`,
  joint hiding state commitments `C_i=Com(cid,D,bal_i,N_{i+1};r_i)`.
- **Post-quantum signatures** (`wots.py`): Winternitz OTS wrapped in an
  XMSS-lite Merkle tree so the router (Bob) countersigns many commitments
  under one public root. Verification is pure hashing — cheap in a STARK.
- **The payment relation R_pay** (`relation.py`): genesis/signed-parent
  disjunction, chain equation, `bal<=D` cap, output binding. The prover is
  **swappable**; `ClearWitnessProver` is the reference (sound, not ZK). A
  STARK backend drops in behind the same interface — `check_R_pay` is the
  circuit spec.
- **Channel state machine + referee** (`channel.py`): open, pay/countersign
  with global nullifier dedup, three close modes (genesis/signed/unsigned),
  the challenge game with mode-dependent exhibit sets, settlement, and
  timeout-forfeit.

## Trust properties (all covered by `tests/test_confetti.py`, 12 tests)

- Honest pay→close→settle pays Bob exactly `bal`, Alice `D-bal`.
- Stale close (closing a non-tip state) → nullifier collision → Bob challenges
  → Alice forfeits the whole deposit.
- A fully honest close cannot be challenged (attribution-free: nothing it
  publishes ever appeared in a message).
- Dedup: Bob refuses to countersign a repeated nullifier (forks die).
- Overspend capped at `D`; forged countersignatures and forged Bob signatures
  rejected; challenge after the window rejected.
- Liveness: Alice recovers her full deposit via genesis close if Bob never
  signs; Bob claims the deposit if Alice goes AWOL.

## Where the ZK gap is (and why it's isolated)

`ClearWitnessProver` provides **knowledge soundness** (every constraint is
checked) but not **zero-knowledge** — the payment witness travels in the
clear. That is the *only* missing property, and it is confined to one class
behind the `Prover` interface. The Phase-0 benchmark
(`research/phase0-proving-benchmark.md`) measured the real STARK cost:
**MARGINAL, ~25 s median** on an M4, viable via proof pipelining (payment
i+1 depends only on the parent, so it proves in the background during the
user's think-time). Swapping `Sp1Prover` in changes nothing else in the stack.

## Off-chain vs on-chain (M4a vs M4b)

The `Contract` class is an in-memory referee: registry, closes, challenge,
settlement all in one process, reset on restart, deposits still custodial.
**M4b** replaces it with a real contract (Base Sepolia or the Cleave Anvil
testnet) and a Groth16/PLONK-wrapped STARK verifier for cheap on-chain
verification — at which point deposits leave custody and the trust
minimization is real. The protocol logic (this package) does not change.

## Router integration

- `GET /channel/params` → `pk_B`, current `root`, flat `price_per_request`.
- `POST /channel/open` → registers the channel record, returns the membership
  path and root.
- `POST /v1/chat/completions` with an `X-Channel-Payment` header → the router
  verifies R_pay, dedups, countersigns (returned in `X-Channel-Countersign`),
  and proxies upstream.

CLI: `cli.py channel open <credits>`, `cli.py channel status`,
`cli.py chat "..." --channel`.

Pricing is a flat per-request delta in M4a; per-token metered channel pricing
is M4b work.
