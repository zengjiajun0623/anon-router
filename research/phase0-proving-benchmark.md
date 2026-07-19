# Phase 0: Client-Side Proving Benchmark (Confetti Payment Proof)

**Date:** 2026-07-19
**Question:** Is per-request client-side proving viable on a consumer laptop for confetti payment-channel payments (anon-router phase 0 gate)?
**Verdict: MARGINAL** — median core prove time 25.4 s on an Apple M4 laptop (10–60 s band). Not interactive per-request as-is, but workable with proof pipelining; see Mitigations.

## Machine and versions

| Item | Value |
|---|---|
| Machine | Apple M4, 10 cores, 16 GB RAM, macOS 26.5.2 |
| zkVM | SP1 (Succinct), `cargo-prove sp1 (8252c29 2026-06-25)` via sp1up |
| SP1 crates (resolved) | sp1-sdk 6.3.1, sp1-zkvm 6.3.1 |
| Guest sha256 | SP1 precompile via `sha2` patch `patch-sha2-0.10.8-sp1-4.0.0` (sp1-patches/RustCrypto-hashes) |
| Host rustc | 1.97.1; guest toolchain `succinct` rustc 1.94.0-dev |
| Prover | CPU only (`ProverClient::from_env()`, local CPU prover; no GPU, no network) |

Benchmark project: `~/cleavelabs/anon-router/research/phase0-bench/confetti-bench/` (guest: `program/src/main.rs`, host: `script/src/bin/main.rs`). Reused from the prior attempt's scaffold; toolchain was already installed by that attempt.

## Workload (approximates one PROTOCOL.md "Payment" proof)

1. **WOTS one-time signature verify** (Bob's signature on the parent state, verified inside the proof): w=16, 67 sha256 hash chains (64 message nibbles + 3 checksum), verifier walks each chain to the top and hashes the 67 tops into the pk. Measured average 6.72 iterations/chain this run (expected 7.5; spec said ~8).
2. **4 sha256 commitments** over 32-byte inputs (balance commitment, next-nullifier commitment, etc.).
3. **Balance checks on u64:** `parent_balance + delta == new_balance` (checked add) and `new_balance <= D`.

Public values: delta, WOTS pk, 4 commitments. Witness (msg, sig, balances) private. A valid signature is generated host-side and verified in-guest (proof fails on an invalid one; the assert is in the execution path).

## Exact commands

```bash
# toolchain (already present from prior attempt; original install was)
curl -L https://sp1up.succinct.xyz | bash && sp1up

# build + run
cd ~/cleavelabs/anon-router/research/phase0-bench/confetti-bench
export PATH="$HOME/.sp1/bin:$PATH"
cargo build --release
RUST_LOG=off ./target/release/bench                        # core proofs
RUST_LOG=off BENCH_MODE=compressed ./target/release/bench  # compressed proofs
```

## Results

**Guest cycles:** 2,072,556 total instructions (~2.07 M) — with the sha256 precompile.
**Setup (one-time per program, amortized):** 1.01 s.

### Core (STARK) proofs — 3 runs

| Run | Prove (s) | Proof size (bytes) | Verify (s) |
|---|---|---|---|
| 1 | 25.57 | 2,805,079 | 0.060 |
| 2 | 24.88 | 2,805,079 | 0.058 |
| 3 | 25.41 | 2,805,079 | 0.060 |
| **Median** | **25.41** | **2,805,079 (~2.8 MB)** | **0.060** |

### Compressed (recursed, constant-size) proofs — 3 runs

SP1 "compressed" mode recurses the core shards down to a constant-size proof; it is the required input to the Groth16/PLONK EVM wrapper and the form you'd relay if 2.8 MB per payment is too much bandwidth.

| Run | Prove (s) | Proof size (bytes) | Verify (s) |
|---|---|---|---|
| 1 | 56.10 | 1,272,737 | 0.025 |
| 2 | 53.61 | 1,272,737 | 0.025 |
| 3 | 55.75 | 1,272,737 | 0.026 |
| **Median** | **55.75** | **1,272,737 (~1.27 MB)** | **0.025** |

Observed prover footprint during compressed runs: ~700–930% CPU (all performance cores), peak ~9.3 GB RSS with ~5.9 GB swap in use on this 16 GB machine — compressed mode is at the edge of a 16 GB laptop's comfort zone; core mode is lighter.

### Groth16/PLONK (EVM-verifiable) wrapper

**Not measurable on this machine.** SP1's Groth16/PLONK wrapping uses gnark; without the `native` feature (x86_64-linux only, needs a Go build) the SDK shells out to the `sp1-gnark` Docker image (`sp1-recursion-gnark-ffi` `src/ffi/mod.rs` — `native` feature or Docker fallback), and Docker is not installed here. Reference points from Succinct's published numbers: the wrap adds roughly tens of seconds to ~2 min of CPU work on top of the compressed proof, yields a ~260-byte Groth16 proof, ~270 k gas to verify onchain. Wrapping is only needed for onchain settlement (open/close/challenge), **not** for the per-request payment path — Bob can verify STARK/compressed proofs natively off-chain.

## Verdict

**MARGINAL** (10 s < 25.4 s < 60 s). Straight-line per-request proving adds ~25 s latency per payment on a current consumer laptop (core STARK; ~56 s if the constant-size compressed proof is required per payment) — too slow for interactive request/response, not so slow the design is dead. The decisive number: **median core prove = 25.4 s**.

## Mitigations

- **Proof pipelining (primary).** The payment proof for state i+1 depends only on the parent state and Bob's signature on it — not on the next request's content, since delta is typically the flat per-request price. Alice can prove the next payment in the background while the current request is being served; steady-state added latency ≈ 0 as long as requests arrive ≥ ~25 s apart. Burst traffic still queues at ~2.4 proofs/min/core-set.
- **Precompute at t=0.** The first payment (extends genesis) can be proven before the first request is ever made.
- **GPU proving.** SP1 CUDA prover (Linux/NVIDIA) is typically ~5–10× faster than CPU — ~3–5 s/proof, but that is not "consumer laptop on macOS."
- **Batching.** Amortize one proof over k payments (pay-per-k-requests, or one proof covering k chain steps). Cycles are dominated by the ~500 sha256 calls of the WOTS verify; k steps in one proof scales sub-linearly in prove time until the next power-of-2 shard boundary.
- **Smaller workload.** ~2.07 M cycles is modest; prove time is in SP1's fixed-ish small-program regime, so shrinking the guest (e.g. cheaper OTS parameters) yields limited gains. A hash-native proof system (e.g. Plonky3-based custom AIR, or a dedicated WOTS circuit) would cut this dramatically at the cost of engineering effort.

## Caveats

- This is an **approximation** of the payment proof: it verifies one WOTS signature, 4 commitments, and the balance relation. The real Spec-v2 statement may differ (e.g. commitment openings inside the proof, nullifier-chain derivation, "genesis OR signed-parent" disjunction — the disjunction roughly doubles the hashing in the worst case, still far below shard limits).
- Verify times are for native (Rust) verification, which is the recipient's per-request path; EVM verification needs the (unmeasured here) Groth16 wrap, and only at settlement.
- 16 GB RAM sufficed for core mode; compressed mode pushed into swap (~9.3 GB RSS peak) yet stayed within ~10% run-to-run variance.
- Proof size 2.8 MB (core) is per-payment network overhead Bob must receive; compressed mode halves it to a constant 1.27 MB at ~2.2× the prove time.
- Single machine, single day, plugged-in; thermals on a fanless/mobile chip could add ~10–20 %.
- Prior attempt's scaffold reused as-is after review; numbers here are from fresh runs on 2026-07-19.
