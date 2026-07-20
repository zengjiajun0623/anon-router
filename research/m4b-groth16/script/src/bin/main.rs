//! M4b host: prove the R_pay genesis-branch guest over the Python-generated
//! fixture, wrap to Groth16 (Docker gnark), and emit the on-chain fixture
//! (vkey hash, ABI public values, proof bytes) for the Foundry test.
//!
//! Usage:  cargo run --release [--] [execute|core|groth16]

use serde::Deserialize;
use sp1_sdk::{
    blocking::{ProveRequest, Prover, ProverClient},
    include_elf, Elf, HashableKey, ProvingKey, SP1Stdin,
};
use std::{fs, path::Path, time::Instant};

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
    // Signed-branch fields (Phase 4); dummies are filled in when absent so a
    // genesis fixture without them still drives the full-disjunction guest.
    #[serde(rename = "C_prev", default)]
    c_prev: Option<String>,
    #[serde(default)]
    r_prev: Option<String>,
    #[serde(default)]
    sig_index: Option<u32>,
    #[serde(default)]
    wots_sig: Option<String>,
    #[serde(default)]
    auth_path: Option<String>,
}

fn dummy_bytes(seed: &[u8], label: &str, n: usize) -> Vec<u8> {
    use sha2::{Digest, Sha256};
    let mut out = Vec::with_capacity(n);
    let mut ctr: u32 = 0;
    while out.len() < n {
        let mut m = Sha256::new();
        m.update(b"rpay-dummy");
        m.update(label.as_bytes());
        m.update(ctr.to_be_bytes());
        m.update(seed);
        out.extend_from_slice(&m.finalize());
        ctr += 1;
    }
    out.truncate(n);
    out
}

#[derive(Deserialize)]
struct Fixture {
    statement: JStatement,
    witness: JWitness,
}

fn hx(s: &str) -> Vec<u8> {
    hex::decode(s).expect("bad hex in fixture")
}

fn main() {
    sp1_sdk::utils::setup_logger();
    let mode = std::env::args().nth(1).unwrap_or_else(|| "groth16".into());

    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().to_path_buf();
    let fx: Fixture =
        serde_json::from_str(&fs::read_to_string(dir.join("fixture.json")).expect(
            "fixture.json missing — run: python3 research/m4b-groth16/make_fixture.py",
        ))
        .unwrap();

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
    // signed-branch fields (dummies when absent — genesis fixture)
    let seed = hx(&fx.witness.r_i);
    let opt = |v: &Option<String>, label: &str, n: usize| -> Vec<u8> {
        v.as_ref().map(|s| hx(s)).unwrap_or_else(|| dummy_bytes(&seed, label, n))
    };
    stdin.write_vec(opt(&fx.witness.c_prev, "C_prev", 32));
    stdin.write_vec(opt(&fx.witness.r_prev, "r_prev", 32));
    stdin.write(&fx.witness.sig_index.unwrap_or(0x0aa));
    stdin.write_vec(opt(&fx.witness.wots_sig, "wots_sig", 67 * 32));
    stdin.write_vec(opt(&fx.witness.auth_path, "auth_path", 12 * 32));

    let client = ProverClient::from_env();

    let (pv, report) = client.execute(ELF, stdin.clone()).run().expect("guest rejected witness");
    println!("guest OK, cycles = {}", report.total_instruction_count());
    println!("public values ({} bytes) = 0x{}", pv.as_slice().len(), hex::encode(pv.as_slice()));
    if mode == "execute" {
        return;
    }

    let t = Instant::now();
    let pk = client.setup(ELF).expect("setup failed");
    let vk = pk.verifying_key().clone();
    println!("setup: {:.1}s, vkey = {}", t.elapsed().as_secs_f64(), vk.bytes32());

    let t = Instant::now();
    let proof = if mode == "core" {
        client.prove(&pk, stdin).run().expect("core prove failed")
    } else {
        client.prove(&pk, stdin).groth16().run().expect("groth16 prove failed")
    };
    println!("prove ({mode}): {:.1}s", t.elapsed().as_secs_f64());

    client.verify(&proof, &vk, None).expect("host-side verify failed");
    println!("host-side verify: OK");

    if mode == "groth16" {
        let proof_bytes = proof.bytes();
        println!("onchain proof ({} bytes) = 0x{}", proof_bytes.len(), hex::encode(&proof_bytes));
        let fixture = serde_json::json!({
            "vkey": vk.bytes32(),
            "publicValues": format!("0x{}", hex::encode(proof.public_values.as_slice())),
            "proof": format!("0x{}", hex::encode(&proof_bytes)),
        });
        let out = dir.join("groth16-fixture.json");
        fs::write(&out, serde_json::to_string_pretty(&fixture).unwrap()).unwrap();
        println!("wrote {}", out.display());
    }
}
