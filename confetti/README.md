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
clear. It is confined to one class behind the `Prover` interface, and as of
Phase 1 it is the dev/test double only (`CHANNEL_PROVER=clear`).

**Phase 1 (done): `RealSP1Prover` (`sp1.py`)** closes the gap for the
**genesis branch** — the first payment on a channel carries a real SP1 core
STARK (~2.8 MB) produced by the `rpay` host binary
(`research/m4b-groth16/`, guest = the exact byte-compatible genesis branch
of `check_R_pay`). Measured on an M4 laptop: **~25 s prove wall
(11-12 s core STARK)**, **~0.5 s router-side verify** (LightProver + cached
vkey; statement binding checked against `abi(delta, N_i, C_i, root)` inside
the binary). The witness never leaves the client. Router selects the backend
with `CHANNEL_PROVER=sp1|clear` and advertises it in `/channel/params`;
SP1 payments ride in the `_channel_payment` body field (too big for a
header). Proof pipelining (payment i+1 depends only on the parent, so it
proves during the user's think-time) hides the latency in steady state.

**Phase 4 (open): SignedBranch** — non-genesis payments still need
`xmss_verify` inside the guest (~2 M cycles per the Phase-0 bench); until
then `RealSP1Prover.prove` raises `NotImplementedError` past payment #1.

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
