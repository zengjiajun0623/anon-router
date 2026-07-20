# ConfettiChannels — on-chain escrow + referee (M4b)

The M4b milestone: deposits leave the router's custody and live in this
contract; close / challenge / settlement are enforced by chain code, not the
operator's process. Reference protocol: `../../zk-payments-confetti/Spec-v2.md`.

## Contracts

- `src/ConfettiChannels.sol` — escrow + registry + close/challenge/settle state
  machine. Deposits in ETH; per-channel epoch-quantized Merkle registry root.
- `src/IVerifier.sol` — swappable proof verifier. `verifyPayment`,
  `verifyGenesisClose`, `verifySignedClose`, `verifyCloseUnsigned`.
- `src/MockVerifier.sol` — accepts everything. **LOCAL DEMO ONLY** — never on a
  network with real value. The real SP1 Groth16-wrapped verifier implements the
  same interface (M4b-real, Docker-gated).

## Lifecycle

`open` (deposit) → off-chain payments (see `../confetti/`) → `closeGenesis`
/`closeSigned`/`closeUnsigned` (starts a `tau` challenge window) →
`challenge` (Bob, within window, forfeits Alice's deposit on a stale close) →
`finalize` (either party, after the window) → `withdraw` (pull-payment).
`timeoutForfeit` lets Bob claim the deposit if Alice never closes.

## Trust property

The operator (Bob) cannot freeze or steal a payer's funds: every payout- or
challenge-deciding value is a proof-bound public input checked against on-chain
channel state, the challenge window is on-chain block time, and payouts are a
pull-payment ledger. Bob's worst case is receiving the whole deposit, which
requires either a valid fraud challenge or an AWOL payer.

## Review outcome (before any real deployment)

Three-reviewer gate. Two returned; **all findings fixed and regression-tested**:

- **Fable 5** (`../research/m4b-fable-review-and-design.md`): 1 critical
  (unsigned-close payout unbound to the proof — theft), + epoch-root anonymity,
  signed-branch chain-secret binding, Bob-state persistence, cid-uniqueness.
- **Codex**: 1 critical (intra-epoch `open` moved the accepted root → challenge
  front-run/theft), 1 high (cid reuse after settle locks funds), 1 medium
  (push-payment griefing). Fixed via write-once epoch roots, permanent
  cid-uniqueness, and pull-payments.
- **Kimi K2**: not run — OAuth expired (`cliproxyapi -kimi-login` to restore).

Known-open (documented, not blocking the local demo): the `tau` (7d) vs
2-epoch root-accept window is a protocol-level tension for the Spec freeze;
Bob's off-chain state needs durable persistence before mainnet; the real
Groth16 verifier is Docker-gated. Bob's challenge is operator-funded and
event-driven — a liveness SLO to monitor.

## Run

```bash
export PATH="$HOME/.foundry/bin:$PATH"
forge test                      # 15 tests: escrow, 3 closes, challenge, forfeit, griefing
anvil --silent &                # local chain
# deploy MockVerifier then ConfettiChannels(verifier, tau, tAbs, tReq, tRoot)
python ../demo_m4b.py <addr>    # full lifecycle: honest split + fraud-forfeit
```
