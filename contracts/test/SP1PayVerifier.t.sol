// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {SP1PayVerifier} from "../src/SP1PayVerifier.sol";
import {MockVerifier} from "../src/MockVerifier.sol";
import {IVerifier} from "../src/IVerifier.sol";
import {ISP1Verifier} from "sp1-contracts/contracts/src/ISP1Verifier.sol";
import {SP1Verifier} from "sp1-contracts/contracts/src/v4.0.0-rc.3/SP1VerifierGroth16.sol";

/// @dev SP1's REAL published Groth16 proof (canonical Fibonacci example),
///      vendored from sp1-contracts/contracts/test/SP1VerifierGroth16.t.sol.
///      Its 4-byte selector (0x11b6a09d) matches SP1VerifierGroth16 v4.0.0-rc.3,
///      so this is a genuine Groth16 proof that the vendored on-chain verifier
///      accepts. Mirrored at contracts/test/fixtures/sp1-canonical-groth16.json.
library SP1Fixture {
    function programVKey() internal pure returns (bytes32) {
        return 0x00562c19b1948ce8f360ee32da6b8e18b504b7d197d522085d3e74c072e0ff7d;
    }

    function publicValues() internal pure returns (bytes memory) {
        return
            hex"00000000000000000000000000000000000000000000000000000000000000140000000000000000000000000000000000000000000000000000000000001a6d0000000000000000000000000000000000000000000000000000000000002ac2";
    }

    function proof() internal pure returns (bytes memory) {
        return
            hex"11b6a09d15c0a8f6b56f8226262eccb0d78ab7946001762a2a9117b0ce6626ee0f15338a164391b8e4af70b9ad5f80df72a2fd42038afc66190edd82bf1f0d752ce22ab208f5de7a1c73d97f82e989add997eca2e95af1716a5d9c03cbcec2bb477aa06d00b7de11d8465f44fc1073d49a2809a57d31ad543a3602be355ea05aedf894aa0839ad0113478bf84a25faff25306a84185c20d1320772e4769d993832626f081e432d60d8f4cb6f82f8835872aa0c3183ffe09f67d365951722c1a3debd6ae90c31023395fe16b29c3a01524447de9e22aa670c6a7cd880281ba14c642a601b0530706caf4af3644ff20a785ac0e499321f08cfc96cee48b64bfa08925ec27c";
    }
}

/// @notice PART 1 — the REAL on-chain Groth16 verification primitive.
///
/// Proves that the exact call `SP1PayVerifier.verifyPayment` depends on —
/// `ISP1Verifier.verifyProof(vkey, publicValues, proof)` doing a genuine
/// Groth16 pairing check — accepts a REAL SP1 Groth16 proof on-chain and
/// rejects a tampered proof / tampered public values. The proof is SP1's
/// canonical Fibonacci fixture (see SP1Fixture), NOT our R_pay proof (our own
/// R_pay Groth16 proof is blocked on local prover memory; see
/// research/m4b-real-groth16.md). This is the security-critical primitive; the
/// wiring on top of it is exercised in Part 2.
contract SP1PayVerifierRealGroth16Test is Test {
    SP1Verifier internal sp1;

    function setUp() public {
        sp1 = new SP1Verifier();
    }

    /// A real Groth16 proof verifies on-chain (no revert == accepted).
    function test_realGroth16ProofVerifiesOnChain() public view {
        sp1.verifyProof(SP1Fixture.programVKey(), SP1Fixture.publicValues(), SP1Fixture.proof());
    }

    /// Report the on-chain gas of the real Groth16 verify.
    function test_gas_realGroth16Verify() public {
        uint256 g = gasleft();
        sp1.verifyProof(SP1Fixture.programVKey(), SP1Fixture.publicValues(), SP1Fixture.proof());
        emit log_named_uint("groth16_verify_gas", g - gasleft());
    }

    /// A bit-flip inside the proof body makes the pairing check fail.
    function test_rejectsTamperedProof() public {
        bytes memory bad = SP1Fixture.proof();
        bad[8] ^= 0x01; // byte 8: first byte past the 4-byte selector
        vm.expectRevert();
        sp1.verifyProof(SP1Fixture.programVKey(), SP1Fixture.publicValues(), bad);
    }

    /// Changing the public values (hence their sha256 digest) makes it fail.
    function test_rejectsTamperedPublicValues() public {
        bytes memory badPV = SP1Fixture.publicValues();
        badPV[31] ^= 0x01; // perturb the first committed word
        vm.expectRevert();
        sp1.verifyProof(SP1Fixture.programVKey(), badPV, SP1Fixture.proof());
    }

    /// A wrong program vkey makes the pairing check fail.
    function test_rejectsWrongVKey() public {
        vm.expectRevert();
        sp1.verifyProof(keccak256("not the program"), SP1Fixture.publicValues(), SP1Fixture.proof());
    }

    /// Routing SP1's real proof THROUGH SP1PayVerifier.verifyPayment against the
    /// REAL verifier exercises the contract's forward+try/catch on the reject
    /// path: verifyPayment re-encodes (delta,N,C,root) into 128 bytes, which can
    /// never equal SP1's 96-byte Fibonacci public values, so the real pairing
    /// check fails and the contract returns false (never reverts). The TRUE path
    /// with a real proof needs our own R_pay proof — the documented gap.
    function test_verifyPayment_realVerifier_rejectPathReturnsFalse() public {
        SP1PayVerifier pay =
            new SP1PayVerifier(address(sp1), SP1Fixture.programVKey(), address(new MockVerifier()));
        bool ok = pay.verifyPayment(
            1000,
            keccak256("N_i"),
            keccak256("C_i"),
            keccak256("root"),
            SP1Fixture.proof()
        );
        assertFalse(ok, "verifyPayment must return false (not revert) when the real proof rejects");
    }
}

/// @notice PART 2 — SP1PayVerifier wiring against the real ISP1Verifier ABI.
///
/// Proves that `verifyPayment` (a) re-encodes exactly
/// `abi.encode(delta, N_i, C_i, root)` as the public values, (b) forwards them
/// with the configured program vkey to the injected `ISP1Verifier`, (c) returns
/// `true` iff the verifier accepts and `false` (never reverts) if it rejects,
/// and (d) routes the not-yet-ported relations to the fallback verifier.
///
/// The stub verifier below is authenticating, not permissive: it accepts ONLY
/// when the forwarded (vkey, publicValues) are bit-identical to the expected
/// re-encoding, so a passing `true` test is real evidence the contract encodes
/// and forwards correctly — the piece that will carry OUR R_pay proof once it
/// can be generated on a large-enough host.
contract AuthenticatingStubVerifier is ISP1Verifier {
    bytes32 public immutable expectedVKey;
    bytes32 public immutable expectedDigest;

    constructor(bytes32 vkey, bytes memory expectedPublicValues) {
        expectedVKey = vkey;
        expectedDigest = keccak256(expectedPublicValues);
    }

    /// Reverts (as SP1's real verifier does on a bad proof) unless the forwarded
    /// bytes match exactly. Ignores `proofBytes` — Part 1 already proves the
    /// real pairing check; this isolates the contract's encode+forward logic.
    function verifyProof(bytes32 programVKey, bytes calldata publicValues, bytes calldata)
        external
        view
    {
        require(programVKey == expectedVKey, "wrong vkey forwarded");
        require(keccak256(publicValues) == expectedDigest, "wrong public values forwarded");
    }
}

contract SP1PayVerifierForwardingTest is Test {
    uint256 constant DELTA = 1000;
    bytes32 constant VKEY = bytes32(uint256(0xB0B));

    bytes32 nI = keccak256("N_i");
    bytes32 cI = keccak256("C_i");
    bytes32 root = keccak256("root");

    /// verifyPayment returns TRUE exactly when the injected verifier accepts the
    /// re-encoded statement — proving the encode + forward is correct.
    function test_verifyPayment_forwardsAndReturnsTrue() public {
        bytes memory expectedPV = abi.encode(DELTA, nI, cI, root);
        AuthenticatingStubVerifier stub = new AuthenticatingStubVerifier(VKEY, expectedPV);
        SP1PayVerifier pay = new SP1PayVerifier(address(stub), VKEY, address(0));
        assertTrue(pay.verifyPayment(DELTA, nI, cI, root, hex"00"));
    }

    /// Any perturbed public input changes the forwarded encoding, so the
    /// authenticating verifier reverts and verifyPayment returns false.
    function test_verifyPayment_rejectsWrongStatement() public {
        bytes memory expectedPV = abi.encode(DELTA, nI, cI, root);
        AuthenticatingStubVerifier stub = new AuthenticatingStubVerifier(VKEY, expectedPV);
        SP1PayVerifier pay = new SP1PayVerifier(address(stub), VKEY, address(0));
        assertFalse(pay.verifyPayment(DELTA + 1, nI, cI, root, hex"00"));
        assertFalse(pay.verifyPayment(DELTA, keccak256("bad N"), cI, root, hex"00"));
        assertFalse(pay.verifyPayment(DELTA, nI, keccak256("bad C"), root, hex"00"));
        assertFalse(pay.verifyPayment(DELTA, nI, cI, keccak256("bad root"), hex"00"));
    }

    /// A wrong program vkey is forwarded and rejected.
    function test_verifyPayment_rejectsWrongVKey() public {
        bytes memory expectedPV = abi.encode(DELTA, nI, cI, root);
        AuthenticatingStubVerifier stub = new AuthenticatingStubVerifier(VKEY, expectedPV);
        SP1PayVerifier pay = new SP1PayVerifier(address(stub), keccak256("other vkey"), address(0));
        assertFalse(pay.verifyPayment(DELTA, nI, cI, root, hex"00"));
    }

    /// The not-yet-ported relations delegate to the fallback verifier.
    function test_fallbackRoutesOtherRelations() public {
        SP1PayVerifier pay =
            new SP1PayVerifier(address(0xdead), VKEY, address(new MockVerifier()));
        assertTrue(pay.verifyGenesisClose(bytes16(0), bytes32(0), bytes32(0), ""));
        assertTrue(pay.verifySignedClose(bytes16(0), 0, bytes32(0), bytes32(0), 0, ""));
    }

    /// With no fallback wired, the not-yet-ported relations revert (they are not
    /// silently accepted).
    function test_noFallbackReverts() public {
        SP1PayVerifier pay = new SP1PayVerifier(address(0xdead), VKEY, address(0));
        vm.expectRevert(SP1PayVerifier.RelationNotPorted.selector);
        pay.verifyGenesisClose(bytes16(0), bytes32(0), bytes32(0), "");
    }
}
