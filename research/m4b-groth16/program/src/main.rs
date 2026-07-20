//! R_pay guest — GENESIS BRANCH of the confetti payment relation (Spec-v2 §3,
//! reference: confetti/relation.py::check_R_pay).
//!
//! Public inputs (committed, ABI-encoded as 4 x 32-byte words):
//!   delta (uint256 BE), N_i (bytes32), C_i (bytes32), root (bytes32)
//!
//! Private witness: cid, D, c, r_open, C_open, pk_B, bal_prev, bal_i, r_i,
//! rec_index, rec_path (Merkle membership of the channel record in `root`).
//!
//! Constraints enforced (genesis branch of check_R_pay, bit-for-bit the same
//! domain-separated sha256 constructions as confetti/hashes.py):
//!   0. C_open = Com(c ; r_open)                     — chain-secret binding
//!   1. leaf(cid, D, pk_B, C_open) is in `root`      — Merkle membership
//!   2. bal_prev == 0                                — genesis parent
//!   3. N_i == H("null", cid, c)                     — first nullifier
//!   4. N_next = H("null", N_i, c)                   — chain equation
//!   5. bal_i == bal_prev + delta, bal_i <= D        — value update
//!   6. C_i == Com(cid, D, bal_i, N_next ; r_i)      — output binding
//!
//! NOT yet implemented (documented in research/m4b-real-groth16.md): the
//! SignedBranch disjunct (XMSS parent-signature verification). The genesis
//! branch alone is a complete, sound instance of R_pay for first payments.

#![no_main]
sp1_zkvm::entrypoint!(main);

use sha2::{Digest, Sha256};

/// _h(domain, *parts): sha256(domain || (len(p) as u32 BE || p)*)
/// — exactly confetti/hashes.py::_h.
fn dsh(domain: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut m = Sha256::new();
    m.update(domain);
    for p in parts {
        m.update((p.len() as u32).to_be_bytes());
        m.update(p);
    }
    m.finalize().into()
}

/// H(*parts) — hashes.py::H (domain "confetti/H").
fn h(parts: &[&[u8]]) -> [u8; 32] {
    dsh(b"confetti/H", parts)
}

/// commit(*parts; r) — hashes.py::commit (domain "confetti/Com", r first).
fn com(r: &[u8], parts: &[&[u8]]) -> [u8; 32] {
    let mut all: Vec<&[u8]> = Vec::with_capacity(parts.len() + 1);
    all.push(r);
    all.extend_from_slice(parts);
    dsh(b"confetti/Com", &all)
}

/// i2b(x) — 32-byte big-endian integer.
fn i2b(x: u64) -> [u8; 32] {
    let mut b = [0u8; 32];
    b[24..].copy_from_slice(&x.to_be_bytes());
    b
}

pub fn main() {
    // --- Statement (public) ---
    let delta = sp1_zkvm::io::read::<u64>();
    let n_i: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let c_i: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let root: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();

    // --- Witness (private) ---
    let cid: Vec<u8> = sp1_zkvm::io::read_vec(); // 16 bytes
    assert_eq!(cid.len(), 16);
    let d_cap = sp1_zkvm::io::read::<u64>(); // deposit D
    let c: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let r_open: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let c_open: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let pk_b: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let bal_prev = sp1_zkvm::io::read::<u64>();
    let bal_i = sp1_zkvm::io::read::<u64>();
    let r_i: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let rec_index = sp1_zkvm::io::read::<u32>();
    let rec_path: Vec<u8> = sp1_zkvm::io::read_vec(); // n * 32 bytes
    assert_eq!(rec_path.len() % 32, 0);

    // 0. Chain-secret binding: C_open = Com(c ; r_open).
    assert_eq!(com(&r_open, &[&c]), c_open, "C_open does not open to committed c");

    // 1. Channel-record membership: leaf in root.
    let leaf = h(&[b"chrec", &cid, &i2b(d_cap), &pk_b, &c_open]);
    let mut node = leaf;
    let mut idx = rec_index;
    for sib in rec_path.chunks_exact(32) {
        node = if idx & 1 == 0 {
            h(&[b"reg-node", &node, sib])
        } else {
            h(&[b"reg-node", sib, &node])
        };
        idx >>= 1;
    }
    assert_eq!(node, root, "genesis: channel record not in root");

    // 2. Genesis parent: bal_prev = 0.
    assert_eq!(bal_prev, 0, "genesis: bal_prev != 0");

    // 3. First nullifier: N_i = H("null", cid, c).
    assert_eq!(h(&[b"null", &cid, &c]), n_i, "genesis: N_i != H(cid, c)");

    // 4. Chain equation: N_next = H("null", N_i, c).
    let n_next = h(&[b"null", &n_i, &c]);

    // 5. Value update (delta is u64, so delta >= 0 by type).
    let sum = bal_prev.checked_add(delta).expect("balance overflow");
    assert_eq!(sum, bal_i, "bal_i != bal_prev + delta");
    assert!(bal_i <= d_cap, "bal_i > D (overspend)");

    // 6. Output binding: C_i = Com(cid, D, bal_i, N_next ; r_i).
    assert_eq!(
        com(&r_i, &[&cid, &i2b(d_cap), &i2b(bal_i), &n_next]),
        c_i,
        "C_i does not bind (cid,D,bal_i,N_next)"
    );

    // --- Public values: abi.encode(uint256 delta, bytes32 N_i, bytes32 C_i, bytes32 root) ---
    sp1_zkvm::io::commit_slice(&i2b(delta));
    sp1_zkvm::io::commit_slice(&n_i);
    sp1_zkvm::io::commit_slice(&c_i);
    sp1_zkvm::io::commit_slice(&root);
}
