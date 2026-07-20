// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IVerifier} from "./IVerifier.sol";

/// @notice Accepts every proof. FOR LOCAL DEMONSTRATION ONLY — it exercises the
///         on-chain custody / close / challenge / settlement mechanics without a
///         real prover. NEVER deploy this to a network holding real value: with
///         it, anyone can forge a close or a challenge. The real SP1 verifier
///         replaces it behind the same interface.
contract MockVerifier is IVerifier {
    function verifyPayment(uint256, bytes32, bytes32, bytes32, bytes calldata)
        external
        pure
        returns (bool)
    {
        return true;
    }

    function verifyCloseUnsigned(
        bytes16,
        uint256,
        bytes32,
        uint256,
        bytes32,
        bytes32,
        uint256,
        bytes32,
        bytes calldata
    ) external pure returns (bool) {
        return true;
    }

    function verifyGenesisClose(bytes16, bytes32, bytes32, bytes calldata)
        external
        pure
        returns (bool)
    {
        return true;
    }

    function verifySignedClose(bytes16, uint256, bytes32, bytes32, uint256, bytes calldata)
        external
        pure
        returns (bool)
    {
        return true;
    }
}
