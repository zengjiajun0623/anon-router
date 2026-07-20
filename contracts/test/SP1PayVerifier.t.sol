// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {SP1PayVerifier} from "../src/SP1PayVerifier.sol";
import {MockVerifier} from "../src/MockVerifier.sol";
import {IVerifier} from "../src/IVerifier.sol";
import {ISP1Verifier} from "sp1-contracts/contracts/src/ISP1Verifier.sol";
import {SP1Verifier} from "sp1-contracts/contracts/src/v4.0.0-rc.3/SP1VerifierGroth16.sol";
import {SP1Verifier as SP1VerifierV6} from "sp1-contracts/contracts/src/v6.1.0/SP1VerifierGroth16.sol";

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

/// @dev OUR OWN real SP1 Groth16 proof of the R_pay genesis-branch guest
///      (research/m4b-groth16/program). Unlike SP1Fixture (SP1's Fibonacci
///      sample), this proof is bound to OUR statement: its two gnark public
///      inputs are exactly programVKey() and sha256(publicValues())&mask.
///      Generated on the RTX 3080 from the Mac-produced gnark witness against
///      the canonical SP1 v6.1.0 trusted-setup circuit; see
///      research/m4b-real-groth16.md and test/fixtures/rpay-groth16.json.
///      `proof()` = 4-byte v6.1.0 selector (0x4388a21c) ++ abi.encode(exitCode,
///      vkRoot, nonce, uint256[8]). Decoded statement: delta=1000, and the
///      N_i / C_i / root below.
library RPayFixture {
    function programVKey() internal pure returns (bytes32) {
        return 0x000be6d3dd1da7bbf5f2884fe2ce6d76a4d579877e8a958dad915c4381ec2cc6;
    }

    // Decoded R_pay statement (challenge arguments) the proof commits to.
    function delta() internal pure returns (uint256) {
        return 1000;
    }

    function nI() internal pure returns (bytes32) {
        return 0x19b4c0bd0770d9a37b35018c9ba54e4a8553ebdfba0357c1bce21849842d06a8;
    }

    function cI() internal pure returns (bytes32) {
        return 0x3f311d313e14afbc2efd21030dfe4627c30e7576df7452f0d44140a2b8c01546;
    }

    function root() internal pure returns (bytes32) {
        return 0x6088f461c4f8c507608b6a40487efdaac8d2f4f124d3571690e287de647ead1f;
    }

    /// abi.encode(delta, N_i, C_i, root) — exactly what verifyPayment re-encodes.
    function publicValues() internal pure returns (bytes memory) {
        return
            hex"00000000000000000000000000000000000000000000000000000000000003e819b4c0bd0770d9a37b35018c9ba54e4a8553ebdfba0357c1bce21849842d06a83f311d313e14afbc2efd21030dfe4627c30e7576df7452f0d44140a2b8c015466088f461c4f8c507608b6a40487efdaac8d2f4f124d3571690e287de647ead1f";
    }

    function proof() internal pure returns (bytes memory) {
        return
            hex"4388a21c0000000000000000000000000000000000000000000000000000000000000000002f850ee998974d6cc00e50cd0814b098c05bfade466d28573240d057f253520000000000000000000000000000000000000000000000000000000000000000186750fb6183d9db01a957afd00970d8bc293a14fd646537a93899a0d4d1139510f87ee52defa6c24fdd9fa207aaea62e1f585715bc3a80271bec22f9be2c4ab0e969dd8dcb2a01f8e438b2e271f37646aa7f6dde0fef7d68432d4ae04bc305a21fc78ce60500a7c5d089c316c28e66170f33d5becea42c452d94dce744d1aab04f655719a8063e1c0ffee07cbd6ca766bda07bce31e7f91adf61dd3f3b896fa08ba6c87290b752db9efaf43fa73f74011f2ede14b00f0f02b8a1c081a31e97b2f4be80b49f173394d5b5a4cf096b77aa982cd0b90224f234ddb83b57d3e7d2a0ea2c23eb7915098a8b0c19676dd7c0d8b1018da816a51787ad26e39587bb954";
    }
}

/// @notice PART 1b — OUR OWN real R_pay Groth16 proof, verified on-chain.
///
/// This is the fixture-vs-fresh gap the earlier milestone could not close: it
/// runs the REAL on-chain BN254 pairing check over a Groth16 proof WE generated
/// (RTX 3080) of OUR R_pay genesis-branch guest, against the vendored
/// SP1VerifierGroth16 v6.1.0 (selector 0x4388a21c) — the exact verifier version
/// whose trusted-setup circuit produced the proof. Crucially it exercises the
/// TRUE path of SP1PayVerifier.verifyPayment with our own proof: constructed
/// with OUR program vkey, verifyPayment(delta, N_i, C_i, root, proof) re-encodes
/// the 128-byte statement, forwards it, the pairing succeeds, and it returns
/// true. Local proving was previously RAM-blocked (research/m4b-real-groth16.md
/// §3); the proof was generated on a 32GB+ host, no contract change required.
contract SP1PayVerifierOurRealGroth16Test is Test {
    SP1VerifierV6 internal sp1;

    function setUp() public {
        sp1 = new SP1VerifierV6();
    }

    /// Sanity: the vendored v6.1.0 verifier is the version our proof targets.
    function test_verifierIsV6() public view {
        assertEq(sp1.VERSION(), "v6.1.0");
        assertEq(bytes4(sp1.VERIFIER_HASH()), bytes4(hex"4388a21c"));
    }

    /// OUR real Groth16 proof of OUR R_pay statement verifies on-chain
    /// (no revert == the pairing check accepted it). This is the exact
    /// ISP1Verifier.verifyProof call that verifyPayment depends on.
    function test_ourRealRPayProofVerifiesOnChain() public view {
        sp1.verifyProof(RPayFixture.programVKey(), RPayFixture.publicValues(), RPayFixture.proof());
    }

    /// THE headline: our own proof routed THROUGH SP1PayVerifier.verifyPayment
    /// on the TRUE path. Constructed with our program vkey; verifyPayment
    /// re-encodes (delta, N_i, C_i, root) to the 128-byte public values, the
    /// real pairing succeeds, and it returns true. address(0) fallback proves
    /// the true result comes from the real verifier, not a fallback.
    function test_verifyPayment_ourRealProof_truePath() public {
        SP1PayVerifier pay =
            new SP1PayVerifier(address(sp1), RPayFixture.programVKey(), address(0));
        bool ok = pay.verifyPayment(
            RPayFixture.delta(),
            RPayFixture.nI(),
            RPayFixture.cI(),
            RPayFixture.root(),
            RPayFixture.proof()
        );
        assertTrue(ok, "our own R_pay Groth16 proof must verify through verifyPayment");
    }

    /// The re-encoded public values must equal exactly the bytes the proof was
    /// generated over (guards the ABI schema the guest commits to).
    function test_verifyPaymentEncodingMatchesProofPublicValues() public pure {
        bytes memory reencoded = abi.encode(
            RPayFixture.delta(), RPayFixture.nI(), RPayFixture.cI(), RPayFixture.root()
        );
        assertEq(keccak256(reencoded), keccak256(RPayFixture.publicValues()));
    }

    /// Report on-chain gas of verifying our real R_pay proof.
    function test_gas_ourRealGroth16Verify() public {
        uint256 g = gasleft();
        sp1.verifyProof(RPayFixture.programVKey(), RPayFixture.publicValues(), RPayFixture.proof());
        emit log_named_uint("rpay_groth16_verify_gas", g - gasleft());
    }

    /// A bit-flip inside our proof body fails the pairing check.
    function test_ourProof_rejectsTamperedProof() public {
        bytes memory bad = RPayFixture.proof();
        bad[8] ^= 0x01; // first byte past the 4-byte selector
        vm.expectRevert();
        sp1.verifyProof(RPayFixture.programVKey(), RPayFixture.publicValues(), bad);
    }

    /// Perturbing our public values (a different R_pay statement) fails.
    function test_ourProof_rejectsTamperedPublicValues() public {
        bytes memory badPV = RPayFixture.publicValues();
        badPV[31] ^= 0x01; // flip a bit in delta
        vm.expectRevert();
        sp1.verifyProof(RPayFixture.programVKey(), badPV, RPayFixture.proof());
    }

    /// A wrong program vkey fails.
    function test_ourProof_rejectsWrongVKey() public {
        vm.expectRevert();
        sp1.verifyProof(keccak256("not our program"), RPayFixture.publicValues(), RPayFixture.proof());
    }

    /// verifyPayment with a perturbed statement returns false (real verifier
    /// rejects the mismatched public values; never reverts).
    function test_verifyPayment_ourProof_rejectsWrongStatement() public {
        SP1PayVerifier pay =
            new SP1PayVerifier(address(sp1), RPayFixture.programVKey(), address(0));
        assertFalse(
            pay.verifyPayment(
                RPayFixture.delta() + 1,
                RPayFixture.nI(),
                RPayFixture.cI(),
                RPayFixture.root(),
                RPayFixture.proof()
            ),
            "a different delta is a different statement and must not verify"
        );
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
