//! rpay — per-payment host CLI for the R_pay genesis-branch SP1 guest.
//!
//! This is the production driver behind `confetti/sp1.py::RealSP1Prover`:
//! the CLI wallet calls `prove` (core STARK, ~25 s on an M4 laptop) and the
//! router calls `verify` (sub-second with a warm vkey cache). The guest ELF
//! is embedded, so verification is pinned to the exact compiled circuit.
//!
//! Usage:
//!   rpay prove  <fixture.json>   <proof.out>   — {statement, witness} JSON in,
//!                                               bincode SP1 core proof out
//!   rpay verify <statement.json> <proof.in>    — exit 0 iff the proof is valid
//!                                               AND its public values equal
//!                                               abi(delta, N_i, C_i, root)
//!   rpay vkey                                  — print the guest vkey hash
//!
//! Timing/result metadata is printed as one JSON object on stdout.

use serde::Deserialize;
use sha2::{Digest, Sha256};
use sp1_sdk::{
    blocking::{ProveRequest, Prover as BlockingProver, ProverClient},
    include_elf, Elf, HashableKey, LightProver, Prover, ProvingKey, SP1ProofWithPublicValues,
    SP1Stdin,
};
use sp1_prover::SP1VerifyingKey;
use std::{fs, path::PathBuf, process::exit, time::Instant};

const ELF: Elf = include_elf!("confetti-rpay-program");

#[derive(Deserialize)]
struct JStatement {
    delta: u64,
    #[serde(rename = "N_i")]
    n_i: String,
    #[serde(rename = "C_i")]
    c_i: String,
    root: String,
}

#[derive(Deserialize)]
struct JWitness {
    cid: String,
    #[serde(rename = "D")]
    d: u64,
    c: String,
    r_open: String,
    #[serde(rename = "C_open")]
    c_open: String,
    #[serde(rename = "pk_B")]
    pk_b: String,
    bal_prev: u64,
    bal_i: u64,
    r_i: String,
    rec_index: u32,
    rec_path: Vec<String>,
}

#[derive(Deserialize)]
struct Fixture {
    statement: JStatement,
    witness: JWitness,
}

fn hx(s: &str) -> Vec<u8> {
    hex::decode(s).expect("bad hex in input JSON")
}

/// abi.encode(uint256 delta, bytes32 N_i, bytes32 C_i, bytes32 root) — must
/// match the guest's commit_slice order exactly.
fn abi_public_values(st: &JStatement) -> Vec<u8> {
    let mut out = Vec::with_capacity(128);
    let mut d = [0u8; 32];
    d[24..].copy_from_slice(&st.delta.to_be_bytes());
    out.extend_from_slice(&d);
    for h in [&st.n_i, &st.c_i, &st.root] {
        let b = hx(h);
        assert_eq!(b.len(), 32, "statement fields must be 32-byte hex");
        out.extend_from_slice(&b);
    }
    out
}

/// vkey cache path: next to the executable, keyed by the ELF hash so a guest
/// rebuild invalidates it automatically.
fn vk_cache_path() -> PathBuf {
    let elf_hash = hex::encode(&Sha256::digest(&*ELF)[..8]);
    let dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(std::env::temp_dir);
    dir.join(format!("rpay-vk-{elf_hash}.bin"))
}

fn read_vk_cache() -> Option<SP1VerifyingKey> {
    let raw = fs::read(vk_cache_path()).ok()?;
    bincode::deserialize::<SP1VerifyingKey>(&raw).ok()
}

fn write_vk_cache(vk: &SP1VerifyingKey) {
    if let Ok(raw) = bincode::serialize(vk) {
        let _ = fs::write(vk_cache_path(), raw); // best-effort cache
    }
}

/// Verifier-side client: LightProver executes/verifies but carries none of the
/// CPU proving machinery, so router-side verification stays sub-second.
fn light_client_and_vk() -> (tokio::runtime::Runtime, LightProver, SP1VerifyingKey, f64, bool) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("tokio runtime");
    let client = rt.block_on(LightProver::new());
    if let Some(vk) = read_vk_cache() {
        return (rt, client, vk, 0.0, true);
    }
    let t = Instant::now();
    let pk = rt.block_on(client.setup(ELF)).expect("setup failed");
    let vk = pk.verifying_key().clone();
    let secs = t.elapsed().as_secs_f64();
    write_vk_cache(&vk);
    (rt, client, vk, secs, false)
}

fn cmd_prove(fixture_path: &str, proof_out: &str) {
    let fx: Fixture = serde_json::from_str(
        &fs::read_to_string(fixture_path).expect("cannot read fixture json"),
    )
    .expect("bad fixture json");

    let mut stdin = SP1Stdin::new();
    // statement
    stdin.write(&fx.statement.delta);
    stdin.write_vec(hx(&fx.statement.n_i));
    stdin.write_vec(hx(&fx.statement.c_i));
    stdin.write_vec(hx(&fx.statement.root));
    // witness
    stdin.write_vec(hx(&fx.witness.cid));
    stdin.write(&fx.witness.d);
    stdin.write_vec(hx(&fx.witness.c));
    stdin.write_vec(hx(&fx.witness.r_open));
    stdin.write_vec(hx(&fx.witness.c_open));
    stdin.write_vec(hx(&fx.witness.pk_b));
    stdin.write(&fx.witness.bal_prev);
    stdin.write(&fx.witness.bal_i);
    stdin.write_vec(hx(&fx.witness.r_i));
    stdin.write(&fx.witness.rec_index);
    let path_flat: Vec<u8> = fx.witness.rec_path.iter().flat_map(|p| hx(p)).collect();
    stdin.write_vec(path_flat);

    let client = ProverClient::from_env();

    // Fast witness check first: a bad witness fails here in <1 s, not at 25 s.
    let (_, report) = match client.execute(ELF, stdin.clone()).run() {
        Ok(r) => r,
        Err(e) => {
            eprintln!("guest rejected witness: {e}");
            exit(2);
        }
    };
    let cycles = report.total_instruction_count();

    let t = Instant::now();
    let pk = client.setup(ELF).expect("setup failed");
    let setup_s = t.elapsed().as_secs_f64();
    // Warm the vk cache for future verify calls.
    let vk = pk.verifying_key().clone();
    write_vk_cache(&vk);

    let t = Instant::now();
    let proof = client.prove(&pk, stdin).run().expect("core prove failed");
    let prove_s = t.elapsed().as_secs_f64();

    client.verify(&proof, &vk, None).expect("self-verify failed");
    proof.save(proof_out).expect("cannot write proof");
    let size = fs::metadata(proof_out).map(|m| m.len()).unwrap_or(0);

    println!(
        "{}",
        serde_json::json!({
            "ok": true,
            "cycles": cycles,
            "setup_s": setup_s,
            "prove_s": prove_s,
            "proof_bytes": size,
            "vkey": vk.bytes32(),
        })
    );
}

fn cmd_verify(statement_path: &str, proof_in: &str) {
    let raw = fs::read_to_string(statement_path).expect("cannot read statement json");
    // Accept either a bare statement object or a {statement: {...}} fixture.
    let st: JStatement = match serde_json::from_str::<Fixture>(&raw) {
        Ok(fx) => fx.statement,
        Err(_) => serde_json::from_str(&raw).expect("bad statement json"),
    };

    let proof = match SP1ProofWithPublicValues::load(proof_in) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("cannot load proof: {e}");
            exit(1);
        }
    };

    let (_rt, client, vk, setup_s, cached) = light_client_and_vk();

    let t = Instant::now();
    if let Err(e) = client.verify(&proof, &vk, None) {
        eprintln!("proof INVALID: {e}");
        exit(1);
    }
    // Statement binding: the proof's committed public values must equal the
    // statement the router is being asked to accept.
    if proof.public_values.as_slice() != abi_public_values(&st).as_slice() {
        eprintln!("proof valid but public values do not match the statement");
        exit(1);
    }
    let verify_s = t.elapsed().as_secs_f64();

    println!(
        "{}",
        serde_json::json!({
            "ok": true,
            "verify_s": verify_s,
            "setup_s": setup_s,
            "vk_cached": cached,
            "vkey": vk.bytes32(),
        })
    );
}

fn main() {
    // Keep prover logs off unless the caller opts in.
    if std::env::var("RUST_LOG").is_ok() {
        sp1_sdk::utils::setup_logger();
    }
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("prove") if args.len() == 4 => cmd_prove(&args[2], &args[3]),
        Some("verify") if args.len() == 4 => cmd_verify(&args[2], &args[3]),
        Some("vkey") => {
            let (_rt, _client, vk, _, _) = light_client_and_vk();
            println!("{}", vk.bytes32());
        }
        _ => {
            eprintln!(
                "usage: rpay prove <fixture.json> <proof.out>\n       \
                 rpay verify <statement.json> <proof.in>\n       \
                 rpay vkey"
            );
            exit(64);
        }
    }
}
