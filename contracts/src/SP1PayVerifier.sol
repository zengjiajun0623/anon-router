// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IVerifier} from "./IVerifier.sol";
import {ISP1Verifier} from "sp1-contracts/contracts/src/ISP1Verifier.sol";

/// @title Real SP1 Groth16 verifier for the R_pay path.
/// @notice Replaces MockVerifier.verifyPayment with an actual on-chain
///         Groth16 verification of the SP1 R_pay program (genesis branch;
///         see research/m4b-groth16/program/src/main.rs).
///
///         The guest commits its public values as
///         abi.encode(uint256 delta, bytes32 N_i, bytes32 C_i, bytes32 root)
///         (four 32-byte words), so re-encoding the challenge arguments here
///         binds the proof to exactly the statement the channel contract is
///         checking. `proof` is the raw SP1 Groth16 proof bytes (4-byte
///         verifier-hash selector + gnark proof).
///
///         The remaining relations (closeUnsigned / genesisClose / signedClose)
///         are not yet ported to SP1 guests; they delegate to a fallback
///         verifier (MockVerifier on the local chain) so the settlement
///         mechanics keep working during the migration. Passing address(0)
///         disables them (reverts).
contract SP1PayVerifier is IVerifier {
    /// @notice SP1VerifierGroth16 (or the SP1VerifierGateway) address.
    ISP1Verifier public immutable sp1Verifier;
    /// @notice The R_pay program verification key (vk.bytes32()).
    bytes32 public immutable payProgramVKey;
    /// @notice Verifier used for the not-yet-ported relations.
    IVerifier public immutable fallbackVerifier;

    error RelationNotPorted();

    constructor(address _sp1Verifier, bytes32 _payProgramVKey, address _fallback) {
        sp1Verifier = ISP1Verifier(_sp1Verifier);
        payProgramVKey = _payProgramVKey;
        fallbackVerifier = IVerifier(_fallback);
    }

    /// @inheritdoc IVerifier
    function verifyPayment(
        uint256 delta,
        bytes32 nI,
        bytes32 cI,
        bytes32 root,
        bytes calldata proof
    ) external view returns (bool) {
        bytes memory publicValues = abi.encode(delta, nI, cI, root);
        try sp1Verifier.verifyProof(payProgramVKey, publicValues, proof) {
            return true;
        } catch {
            return false;
        }
    }

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
    ) external view returns (bool) {
        if (address(fallbackVerifier) == address(0)) revert RelationNotPorted();
        return fallbackVerifier.verifyCloseUnsigned(
            cid, d, root, delta, nX, cX, bal, nNext, proof
        );
    }

    function verifyGenesisClose(bytes16 cid, bytes32 cOpen, bytes32 n1, bytes calldata proof)
        external
        view
        returns (bool)
    {
        if (address(fallbackVerifier) == address(0)) revert RelationNotPorted();
        return fallbackVerifier.verifyGenesisClose(cid, cOpen, n1, proof);
    }

    function verifySignedClose(
        bytes16 cid,
        uint256 d,
        bytes32 pkB,
        bytes32 nNext,
        uint256 bal,
        bytes calldata proof
    ) external view returns (bool) {
        if (address(fallbackVerifier) == address(0)) revert RelationNotPorted();
        return fallbackVerifier.verifySignedClose(cid, d, pkB, nNext, bal, proof);
    }
}
