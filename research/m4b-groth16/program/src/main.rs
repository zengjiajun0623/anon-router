//! R_pay guest — the FULL confetti payment relation (Spec-v2 §3, reference:
//! confetti/relation.py::check_R_pay): the flat disjunction
//!
//!     GenesisBranch  OR  SignedBranch
//!
//! Public inputs (committed, ABI-encoded as 4 x 32-byte words):
//!   delta (uint256 BE), N_i (bytes32), C_i (bytes32), root (bytes32)
//! — identical for both branches: nothing public reveals genesis-vs-signed.
//!
//! Private witness: cid, D, c, r_open, C_open, pk_B, bal_prev, bal_i, r_i,
//! rec_index, rec_path (channel-record membership), PLUS the signed-branch
//! fields C_prev, r_prev, sig_index, wots_sig (67x32), auth_path (h x 32).
//! A genesis payment supplies uniformly random dummies for the signed fields;
//! a signed payment supplies the real parent state + Bob's XMSS signature.
//!
//! Branch hiding: BOTH branch computations run unconditionally on every
//! execution — the genesis check (2 hashes) and the signed check (state
//! commitment + full WOTS/XMSS verify, ~500 hashes) — and the final constraint
//! is `genesis_ok | signed_ok` (non-short-circuit). The WOTS chain work
//! depends only on the base-w digits of C_prev, which is a uniformly random
//! 32-byte value in both cases (a commitment for signed, fresh randomness for
//! genesis), so the cycle-count / trace-shape distributions of the two
//! branches are identical: the proof leaks neither the branch nor the message.
//!
//! Constraints (bit-for-bit the domain-separated sha256 of confetti/hashes.py,
//! confetti/wots.py, confetti/merkle.py):
//!   0. C_open = Com(c ; r_open)                       — chain-secret binding
//!   1. leaf(cid, D, pk_B, C_open) is in `root`        — Merkle membership
//!   2. genesis_ok = (bal_prev == 0) & (N_i == H("null", cid, c))
//!   3. signed_ok  = (C_prev == Com(cid, D, bal_prev, N_i ; r_prev))
//!                 & xmss_verify(pk_B, C_prev, sigma)  — Bob signed the parent
//!   4. genesis_ok | signed_ok                          — the disjunction
//!   5. N_next = H("null", N_i, c)                      — chain equation
//!   6. bal_i == bal_prev + delta, bal_i <= D           — value update
//!   7. C_i == Com(cid, D, bal_i, N_next ; r_i)         — output binding

#![no_main]
sp1_zkvm::entrypoint!(main);

use sha2::{Digest, Sha256};

// WOTS parameters — confetti/wots.py (w=16, 67 chains over sha256).
const W: u32 = 16; // Winternitz parameter
const CHAIN_TOP: u32 = W - 1; // 15 hashes from sk to pk element
const LEN1: usize = 64; // 32 bytes -> 64 nibbles
const LEN: usize = 67; // LEN1 + 3 checksum chains

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

/// Base-w digits of a 32-byte message plus the Winternitz checksum
/// — wots.py::_digits.
fn wots_digits(msg: &[u8; 32]) -> [u8; LEN] {
    let mut d = [0u8; LEN];
    for i in 0..32 {
        d[2 * i] = msg[i] >> 4;
        d[2 * i + 1] = msg[i] & 0x0f;
    }
    let csum: u32 = d[..LEN1].iter().map(|&x| CHAIN_TOP - x as u32).sum();
    d[LEN1] = ((csum >> 8) & 0x0f) as u8;
    d[LEN1 + 1] = ((csum >> 4) & 0x0f) as u8;
    d[LEN1 + 2] = (csum & 0x0f) as u8;
    d
}

/// xmss_verify(root, msg, sig) — wots.py::xmss_verify. Returns whether the
/// recomputed XMSS root equals `root`. Always runs the full chain walk.
fn xmss_verify(
    root: &[u8; 32],
    msg: &[u8; 32],
    sig_index: u32,
    wots_sig: &[u8],
    auth_path: &[u8],
) -> bool {
    let digits = wots_digits(msg);
    // wots_pk_from_sig: walk each chain from digit d_i up to the top (w-1),
    // hashing H("wots-chain", idx u16 BE, height u16 BE, node) per step.
    let mut pk = [[0u8; 32]; LEN];
    for i in 0..LEN {
        let mut v: [u8; 32] = wots_sig[i * 32..(i + 1) * 32].try_into().unwrap();
        let idx_be = (i as u16).to_be_bytes();
        for height in (digits[i] as u32)..CHAIN_TOP {
            let h_be = (height as u16).to_be_bytes();
            v = h(&[b"wots-chain", &idx_be, &h_be, &v]);
        }
        pk[i] = v;
    }
    // leaf = H("wots-leaf", pk_0, ..., pk_66)
    let mut leaf_parts: Vec<&[u8]> = Vec::with_capacity(LEN + 1);
    leaf_parts.push(b"wots-leaf");
    for p in pk.iter() {
        leaf_parts.push(p);
    }
    let mut node = h(&leaf_parts);
    // XMSS auth path: parent = H("xmss-node", left, right).
    let mut idx = sig_index;
    for sib in auth_path.chunks_exact(32) {
        node = if idx & 1 == 0 {
            h(&[b"xmss-node", &node, sib])
        } else {
            h(&[b"xmss-node", sib, &node])
        };
        idx >>= 1;
    }
    node == *root
}

pub fn main() {
    // --- Statement (public) ---
    let delta = sp1_zkvm::io::read::<u64>();
    let n_i: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let c_i: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let root: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();

    // --- Witness (private, common) ---
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

    // --- Witness (private, signed-branch fields; random dummies for genesis) ---
    let c_prev: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let r_prev: [u8; 32] = sp1_zkvm::io::read_vec().try_into().unwrap();
    let sig_index = sp1_zkvm::io::read::<u32>();
    let wots_sig: Vec<u8> = sp1_zkvm::io::read_vec(); // 67 * 32 bytes
    assert_eq!(wots_sig.len(), LEN * 32);
    let auth_path: Vec<u8> = sp1_zkvm::io::read_vec(); // height * 32 bytes
    assert_eq!(auth_path.len() % 32, 0);

    // 0. Chain-secret binding: C_open = Com(c ; r_open). Common to both
    //    branches (relation.py review BUG 3).
    assert_eq!(com(&r_open, &[&c]), c_open, "C_open does not open to committed c");

    // 1. Channel-record membership: leaf in root. Common to both branches
    //    (genesis anchors the record; signed anchors pk_B — same leaf, GN-1).
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
    assert_eq!(node, root, "channel record not in root");

    // 2. Genesis branch: bal_prev = 0 and N_i is the first nullifier.
    //    (`&`, not `&&`: no short-circuit — every hash always executes.)
    let genesis_ok = (bal_prev == 0) & (h(&[b"null", &cid, &c]) == n_i);

    // 3. Signed branch: the parent's committed next-nullifier IS the revealed
    //    N_i (C_prev binds cid, D, bal_prev, N_i), and Bob signed C_prev.
    let c_prev_ok = com(&r_prev, &[&cid, &i2b(d_cap), &i2b(bal_prev), &n_i]) == c_prev;
    let sig_ok = xmss_verify(&pk_b, &c_prev, sig_index, &wots_sig, &auth_path);
    let signed_ok = c_prev_ok & sig_ok;

    // 4. The flat disjunction. Which side held stays inside the guest.
    assert!(genesis_ok | signed_ok, "no valid parent branch");

    // 5. Chain equation: N_next = H("null", N_i, c).
    let n_next = h(&[b"null", &n_i, &c]);

    // 6. Value update (delta is u64, so delta >= 0 by type).
    let sum = bal_prev.checked_add(delta).expect("balance overflow");
    assert_eq!(sum, bal_i, "bal_i != bal_prev + delta");
    assert!(bal_i <= d_cap, "bal_i > D (overspend)");

    // 7. Output binding: C_i = Com(cid, D, bal_i, N_next ; r_i).
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
