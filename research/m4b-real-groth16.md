# M4b — Real Groth16 proof for R_pay, wired on-chain

Date: 2026-07-19. Goal: replace `MockVerifier` on the R_pay (challenge) path
with a REAL on-chain-verifiable SP1 Groth16 proof, end to end on the Mac.

## 1. Docker on macOS (Apple Silicon)

No Docker Desktop (license/GUI). Installed a headless daemon:

```
brew install colima docker
colima start --vm-type vz --vz-rosetta --cpu 8 --memory 12
```

- `docker info` OK (Server 29.5.2, Ubuntu 24.04 VM).
- Gotcha 1: `ghcr.io/succinctlabs/sp1-gnark:v6.1.0` is **amd64-only** — no
  arm64 manifest. Fix: VZ + Rosetta VM (`--vz-rosetta`) and
  `docker pull --platform linux/amd64`. Rosetta runs the amd64 gnark binary
  near-natively; the default qemu VM would have been much slower.
- Gotcha 2: first colima start was qemu (default); had to
  `colima delete -f` and restart with `--vm-type vz --vz-rosetta`.
- Image is small (162 MB); SP1's `docker.rs` invokes
  `docker run ghcr.io/succinctlabs/sp1-gnark:$SP1_CIRCUIT_VERSION` (circuit
  version **v6.1.0** for the installed sp1-sdk 6.3.1; override with
  `SP1_GNARK_IMAGE`).

## 2. What the SP1 guest proves (vs the full R_pay relation)

Project: `research/m4b-groth16/` (program + script, patterned on the phase-0
bench). Guest: `program/src/main.rs` — the **genesis branch of R_pay**
(reference: `confetti/relation.py::check_R_pay`), with the exact
domain-separated sha256 constructions from `confetti/hashes.py`
(`confetti/H`, `confetti/Com`, 4-byte BE length prefixes — bit-for-bit
compatible; the Python-generated fixture verifies unmodified in the guest).

Public inputs, committed as `abi.encode(uint256 delta, bytes32 N_i,
bytes32 C_i, bytes32 root)` (128 bytes), so the Solidity side re-encodes the
challenge arguments and gets statement binding for free.

Constraints enforced (genesis branch — a complete, sound instance of R_pay
for first payments):

| # | check_R_pay constraint | in guest? |
|---|---|---|
| 0 | `C_open = Com(c ; r_open)` (chain-secret binding) | YES |
| 1 | channel-record leaf `H("chrec", cid, D, pk_B, C_open)` Merkle-member of `root` | YES (full membership, arbitrary depth) |
| 2 | genesis: `bal_prev == 0` | YES |
| 3 | genesis: `N_i == H("null", cid, c)` | YES |
| 4 | chain equation `N_next = H("null", N_i, c)` | YES |
| 5 | `delta >= 0`, `bal_i == bal_prev + delta`, `bal_i <= D` | YES (u64 + checked_add) |
| 6 | output binding `C_i = Com(cid, D, bal_i, N_next ; r_i)` | YES |
| — | **SignedBranch disjunct** (parent = Bob-signed state: `C_prev` opening + XMSS verify) | **NOT YET** |

Missing piece for the FULL relation: the flat disjunction over
{GenesisBranch, SignedBranch}. The SignedBranch adds (a) the parent
commitment opening `C_prev = Com(cid, D, bal_prev, N_i; r_prev)` (one more
`com()` call — trivial) and (b) `xmss_verify(pk_B, C_prev, sigma_prev)` —
WOTS w=16 chain verification + XMSS auth path, which the phase-0 bench guest
already implements against the same sha256 (~67 chains, benchmarked there).
So the remaining guest work is porting `confetti/wots.py::xmss_verify`
byte-compatibly (domains `"wots-chain"` etc. per wots.py) and branching on a
witness tag. No new cryptography, no new host plumbing.

Witness generation: `make_fixture.py` builds a REAL statement/witness with
the reference implementation (registry of 9 channel records, non-trivial
Merkle path, first payment delta=1000 on D=1,000,000) and asserts
`check_R_pay(st, w) is None` before writing `fixture.json`. Guest executed
over it: **106,110 cycles** (tiny; sha256 precompile does the work).

## 3. Groth16 wrap + on-chain verifier

Host (`script/src/bin/main.rs`): execute -> setup -> `prove(...).groth16()`
(STARK core -> compress -> shrink -> wrap natively on arm64, then the gnark
Groth16 step in the amd64 docker image under Rosetta) -> host-side verify ->
writes `groth16-fixture.json` {vkey, publicValues, proof}.

Timings (M-series, 10 cores, 16 GB):

- guest execute: 106,110 cycles
- setup: TBD
- groth16 e2e prove: TBD
- host-side verify: OK/TBD

On-chain side (all under `contracts/`):

- Vendored `lib/sp1-contracts` @ tag **v6.1.0** (must match the circuit
  version, checked via the 4-byte selector = first 4 bytes of
  `VERIFIER_HASH()`); remapping `sp1-contracts/=lib/sp1-contracts/`.
- `src/SP1PayVerifier.sol` — implements `IVerifier`. `verifyPayment`
  re-encodes `(delta, nI, cI, root)` and calls
  `ISP1Verifier(sp1Verifier).verifyProof(payProgramVKey, publicValues,
  proof)` (try/catch -> bool). The three not-yet-ported relations delegate to
  a constructor-supplied fallback verifier (MockVerifier locally;
  `address(0)` disables them with `RelationNotPorted`).
- `test/SP1PayVerifier.t.sol` — deploys the REAL `SP1VerifierGroth16`
  (v6.1.0) + `SP1PayVerifier` with the real program vkey and checks the
  committed proof fixture (`test/fixtures/rpay-groth16.json`): accepts the
  real proof; rejects tampered delta / N_i / C_i / root, a bit-flipped
  proof, and a wrong program vkey.

## 4. Remaining steps to full integration

1. **SignedBranch in the guest**: port `xmss_verify` (wots.py) into the SP1
   guest, add a branch tag to the witness stream, regenerate vkey. The
   phase-0 bench guest already has the WOTS chain loop to lift.
2. **Other three relations**: write guests for R_closeUnsigned,
   R_genesisClose, R_signedClose (same hash library, mostly subsets of
   R_pay's checks) — one program per relation, or one program with a
   relation tag committed as a public input. Then drop the fallback path in
   `SP1PayVerifier` and the MockVerifier entirely.
3. **ConfettiChannels wiring**: deploy `SP1VerifierGroth16` + `SP1PayVerifier`
   (constructor takes the channel contract's existing `IVerifier` slot —
   interface already matches, zero changes to `ConfettiChannels.sol`).
4. **Epoch roots**: the contract must expose the quantized registry roots the
   proofs anchor to (Spec-v2 §3) so `root` in the statement is one the chain
   accepts — currently the challenge path takes `root` as an argument.
5. **Python prover swap**: implement `Sp1Prover` (relation.py `Prover`
   protocol) shelling out to the host binary / prover network, so the wallet
   emits real proofs.
6. **Prover latency**: local groth16 wrap is minutes-scale; for production
   use the Succinct prover network (same proof bytes, same verifier).

## 5. Outcome

See final status in the session log. Artifacts:

- `research/m4b-groth16/` — guest, host, fixture generator, fixtures
- `contracts/src/SP1PayVerifier.sol`, `contracts/test/SP1PayVerifier.t.sol`,
  `contracts/test/fixtures/rpay-groth16.json`, `contracts/lib/sp1-contracts`
