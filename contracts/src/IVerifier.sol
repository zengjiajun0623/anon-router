// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Swappable proof verifier for the confetti channel.
/// @notice The channel contract calls this for every proof it must check.
///         `MockVerifier` (local demo) returns true; the real SP1 Groth16-
///         wrapped verifier implements the same interface, so nothing in
///         `ConfettiChannels` changes when the real prover lands.
///
///         IMPORTANT: putting the clear-witness (M4a reference) prover on
///         chain would publish the witness — a full anonymity break. On chain
///         the proof MUST be the zero-knowledge STARK. The mock exists only so
///         the custody / close / challenge / settlement mechanics can be
///         exercised on a local chain before the wrapped verifier is wired in.
interface IVerifier {
    /// R_pay: public inputs (delta, N_i, C_i, root). Used by challenge.
    function verifyPayment(
        uint256 delta,
        bytes32 nI,
        bytes32 cI,
        bytes32 root,
        bytes calldata proof
    ) external view returns (bool);

    /// R_closeUnsigned: the full R_pay relation for C_x, with bal, N_next and
    /// N_x as PROOF-BOUND public inputs — the circuit constrains
    /// C_x = Com(cid, D, bal, N_next; r) and N_x = revealed parent nullifier.
    /// This is the structural fix for the unsigned-close theft: the payout
    /// balance and exhibit nullifiers cannot be caller-forged.
    function verifyCloseUnsigned(
        bytes16 cid,
        uint256 d,
        bytes32 root,
        uint256 delta,
        bytes32 nX,
        bytes32 cX,
        uint256 bal,
        bytes32 nNext,
        bytes calldata proof
    ) external view returns (bool);

    /// Genesis close: C_open of this channel opens to c and N_1 = H(cid, c).
    function verifyGenesisClose(
        bytes16 cid,
        bytes32 cOpen,
        bytes32 n1,
        bytes calldata proof
    ) external view returns (bool);

    /// Signed close on state x: knowledge of (C, r, sigma) with
    /// C = Com(cid, D, bal, N_{x+1}; r) and Verify(pk_B, C, sigma).
    function verifySignedClose(
        bytes16 cid,
        uint256 d,
        bytes32 pkB,
        bytes32 nNext,
        uint256 bal,
        bytes calldata proof
    ) external view returns (bool);
}
