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

Timings (M-series, 8 cores usable in colima, 16 GB host — run 2026-07-19):

- guest execute: **106,110 cycles**, public values = 128 bytes (matches the
  ABI schema exactly; verified).
- setup (`client.setup(ELF)`): **2.3 s**; program vkey =
  `0x000be6d3dd1da7bbf5f2884fe2ce6d76a4d579877e8a958dad915c4381ec2cc6`
  (REAL, recorded in `groth16-fixture.json`).
- STARK core -> compress -> shrink -> wrap: **completed** natively on arm64.
- gnark Groth16 (docker, Rosetta): reached the container, **read the R1CS in
  1m44s**, then the container was **OOM-killed** (see blocker below).
- **groth16 e2e prove: BLOCKED** at ~442 s wall (the point of failure), no
  proof produced.
- host-side verify: not reached.

### RESOLVED (2026-07-20) — proof generated on the RTX 3080, wired + verified on-chain

The RAM blocker below was resolved by moving only the final gnark Groth16 wrap
to the 32GB+ RTX 3080 (option (a)). The Mac-produced gnark witness
(`groth16_witness.json`, from the STARK core -> compress -> shrink -> wrap that
completed natively) was proved on the 3080 under WSL distro `sp1`, using the
canonical SP1 **v6.1.0** trusted-setup artifacts in `/root/circuit`
(`groth16_pk.bin` 5.8GB, `groth16_vk.bin`, `groth16_circuit.bin` 2.4GB) via the
`sp1-recursion-gnark-cli` `prove` subcommand. Output:
`/mnt/d/wsl/output-test.bin` (1670 bytes, bincode `Groth16Bn254Proof`, sha256
`b20440d7…3941c`).

Provenance checks (all passing, see `contracts/test/fixtures/rpay-groth16.json`):

- The proof's two gnark public inputs are exactly **our** statement:
  `public_input[0]` == program vkey `0x000be6d3…` and `public_input[1]` ==
  `sha256(publicValues) & ((1<<253)-1)` over our 128-byte R_pay public values.
- The proof's `encoded_proof` decodes to `abi.encode(exitCode=0, vkRoot, nonce=0,
  uint256[8])` with `vkRoot == VK_ROOT()` of v6.1.0.
- The vendored `lib/sp1-contracts/.../v6.1.0/Groth16Verifier.sol` embeds the
  identical verifying key to the 3080's `/root/circuit/groth16_vk.bin` (its
  generated `Groth16Verifier.sol` differs only by the `^` in the pragma), so the
  on-chain pairing uses the exact VK that produced the proof.

On-chain proof bytes = the 4-byte v6.1.0 selector `0x4388a21c`
(== `VERIFIER_HASH()[0:4]`) ++ `encoded_proof` = **356 bytes**. This verifies
on-chain against the vendored `SP1VerifierGroth16` v6.1.0 and, crucially, routes
through `SP1PayVerifier.verifyPayment(...)` on the **TRUE path** returning true.
See `contracts/test/SP1PayVerifier.t.sol :: SP1PayVerifierOurRealGroth16Test`
(9 tests) and `RPayFixture`.

- **R_pay Groth16 on-chain verify gas: 231,018** (v6.1.0, our proof).
- `forge test` now: **39 passed, 0 failed** (all suites).

The original blocker analysis is retained below for the record.

### BLOCKER (historical) — gnark Groth16 wrap OOMs on the 16GB Mac

The docker gnark step read the R1CS then died with `Docker command failed`
and no application error (only the `linux/amd64 on arm64` platform warning on
stderr) — the signature of a silent SIGKILL by the VM's OOM killer. Root
cause: SP1's Groth16 wrap circuit needs more RAM than the colima VM has.

- Host: 16 GB Mac. colima VM: **11.65 GiB** (`--memory 12`). macOS needs
  ~4 GB, so the VM cannot be grown enough to fit the gnark prover, which for
  SP1 v6 wants well over the available headroom (SP1 docs: local Groth16
  proving is 16 GB+ minimum, 32 GB+ recommended).
- Not slowness — a hard memory ceiling. Growing colima to 14–15 GiB would
  starve macOS and still OOM the wrap; not attempted.
- **Unblock options:** (a) a >=32 GB host; (b) the Succinct **prover
  network** (same proof bytes, same on-chain verifier — no code change, just
  swap `ProverClient::from_env()` to the network prover). The genesis-branch
  guest, witness, host plumbing, vkey, and public values are all done and
  correct; only the final wrap is gated on RAM.

The SP1 SDK surfaced this indirectly as `artifact not found` (the wrap
stage's output was never produced, so the downstream in-memory artifact
fetch failed) — the underlying cause is the docker OOM above.

## 3b. On-chain path — proven with SP1's REAL published Groth16 proof

> Update 2026-07-20: our own R_pay proof is no longer RAM-blocked (§3 RESOLVED);
> it is now generated on the RTX 3080 and verified on-chain against v6.1.0. The
> SP1 canonical-fixture path below is retained as an independent second proof of
> the primitive (against v4.0.0-rc.3). Both run in the test suite.

Because our own R_pay proof was RAM-blocked at the time, the on-chain
verification path was first proven end-to-end with SP1's canonical published
Groth16 proof (the Fibonacci
example, `contracts/test/fixtures/sp1-canonical-groth16.json`), which is a
GENUINE Groth16 proof. Its 4-byte selector `0x11b6a09d` matches the vendored
`SP1VerifierGroth16` **v4.0.0-rc.3** `VERIFIER_HASH()`, so the vendored
on-chain verifier really accepts it. This is clearly labelled fixture-vs-fresh
throughout: it proves the on-chain **primitive + wiring**, not our R_pay
statement.

On-chain side (all under `contracts/`):

- Vendored `lib/sp1-contracts` carries **both** `v6.1.0` (the version that
  matches our program's circuit, for the future real R_pay proof) and
  `v4.0.0-rc.3` (matches SP1's published fixture, used by the test); remapping
  `sp1-contracts/=lib/sp1-contracts/`.
- `src/SP1PayVerifier.sol` (unchanged, finalized) — implements `IVerifier`.
  `verifyPayment` re-encodes `(delta, nI, cI, root)` into 128-byte public
  values and calls `ISP1Verifier(sp1Verifier).verifyProof(payProgramVKey,
  publicValues, proof)` (try/catch -> bool). The three not-yet-ported
  relations delegate to a constructor-supplied fallback verifier (MockVerifier
  locally; `address(0)` disables them with `RelationNotPorted`).
- `test/SP1PayVerifier.t.sol` — **11 tests, all green** (`forge test
  --match-contract SP1PayVerifier`). Two suites:
  - **PART 1 (`SP1PayVerifierRealGroth16Test`)** — deploys the REAL
    `SP1VerifierGroth16` (v4.0.0-rc.3) and verifies SP1's REAL Groth16 proof
    on-chain (the exact `verifyProof` call `verifyPayment` makes); rejects a
    bit-flipped proof, tampered public values, and a wrong program vkey; and
    routes SP1's real proof THROUGH `verifyPayment` against the real verifier
    on the reject path (returns `false`, never reverts).
  - **PART 2 (`SP1PayVerifierForwardingTest`)** — proves `verifyPayment`
    re-encodes exactly `abi.encode(delta,N_i,C_i,root)` and forwards it with
    the program vkey to the injected `ISP1Verifier`, returning `true` iff the
    verifier accepts (checked with an *authenticating* stub that only accepts
    bit-identical forwarded bytes) and `false` on any perturbed input / wrong
    vkey; plus fallback routing and the no-fallback revert.

### Measured on-chain numbers

- **Groth16 on-chain verify gas: 209,896** (`test_gas_realGroth16Verify`,
  real proof through `SP1VerifierGroth16.verifyProof`).
- **Groth16 proof size: 260 bytes** (4-byte verifier selector + 256-byte
  gnark proof) — constant regardless of program.
- `verifyPayment` adds only the 128-byte ABI re-encode + a `try/catch` over
  that call.
- **Test result: `11 passed; 0 failed`** (2 suites, ~27 ms).

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

## 5. Outcome (2026-07-19 milestone; updated 2026-07-20)

Shippable milestone reached, **genesis-branch only**. The 2026-07-19 RAM gap is
now **closed** (proof generated on the RTX 3080, §3 RESOLVED):

- Guest + witness + host + program vkey + 128-byte public values: **done and
  correct** (execute-verified). vkey `0x000be6d3…`.
- Real R_pay Groth16 proof: **PRODUCED** on the RTX 3080 and **verified
  on-chain** against the vendored `SP1VerifierGroth16` v6.1.0. It is bound to our
  statement (public inputs == our vkey + `sha256(publicValues)&mask`) and routes
  through `SP1PayVerifier.verifyPayment` on the TRUE path, returning true —
  231,018 gas. No contract change was needed to unblock. Fixture:
  `contracts/test/fixtures/rpay-groth16.json`.
- On-chain verification path: **PROVEN** two ways — (i) our own real R_pay proof
  above (v6.1.0), and (ii) SP1's real published Groth16 proof against v4.0.0-rc.3
  (209,896 gas, retained as `SP1Fixture`). Tamper-rejection (bit-flipped proof,
  perturbed public values, wrong vkey) and correct `verifyPayment`
  re-encode/forward are covered for both. `forge test` = **39 passed, 0 failed**
  (of which `--match-contract SP1PayVerifier` = 20 passed).

### Precise remaining gap

1. ~~**Our own R_pay Groth16 proof** (fixture-vs-fresh)~~ — **DONE 2026-07-20**.
   The on-chain TRUE path of `verifyPayment` with *our* proof now executes and
   passes; the wrap ran on a 32GB+ host (RTX 3080), no code change required.
2. **SignedBranch disjunct** (the intended scope cut for this milestone): the
   guest proves only the genesis branch of R_pay — a complete, sound instance
   for first payments. The flat `{GenesisBranch, SignedBranch}` disjunction is
   not yet in the guest; SignedBranch needs the parent-commitment opening plus
   `xmss_verify` (WOTS w=16 chain + XMSS auth path, byte-compatible with
   `confetti/wots.py`; the phase-0 bench guest already has the chain loop).
   No new cryptography or host plumbing — a guest port + vkey regen. Until it
   lands, stale/forked *signed*-state closes are not yet challengeable via
   this on-chain verifier (they still work through the mock on the local
   chain).

Artifacts:

- `research/m4b-groth16/` — guest, host, fixture generator; `fixture.json`
  (statement+witness), `groth16-fixture.json` (our real vkey + public values;
  `proof: null` with blocker note).
- `contracts/src/SP1PayVerifier.sol` (finalized), `contracts/test/
  SP1PayVerifier.t.sol` (11 green tests), `contracts/test/fixtures/
  sp1-canonical-groth16.json` (SP1's real published Groth16 proof, labelled),
  `contracts/lib/sp1-contracts` (vendored v6.1.0 + v4.0.0-rc.3).
