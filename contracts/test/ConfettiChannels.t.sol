// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {ConfettiChannels} from "../src/ConfettiChannels.sol";
import {MockVerifier} from "../src/MockVerifier.sol";
import {IVerifier} from "../src/IVerifier.sol";

contract ConfettiChannelsTest is Test {
    ConfettiChannels ch;
    address payable alice = payable(address(0xA11CE));
    address payable bob = payable(address(0xB0B));

    bytes16 constant CID = bytes16(uint128(0x1234));
    bytes32 constant PKB = bytes32(uint256(0xB0B0));
    bytes32 constant COPEN = bytes32(uint256(0x0DE0));

    uint64 constant TAU = 1 days;

    function setUp() public {
        ch = new ConfettiChannels(IVerifier(new MockVerifier()), TAU, 90 days, 7 days, 1 days);
        vm.deal(alice, 10 ether);
        vm.deal(bob, 1 ether);
    }

    function _open(uint256 dep) internal {
        vm.prank(alice);
        ch.open{value: dep}(CID, bob, PKB, COPEN);
    }

    // deposit sits in the contract, not with the operator
    function test_open_escrows_deposit() public {
        _open(1 ether);
        assertEq(address(ch).balance, 1 ether);
        (uint256 D,,,,,,,) = _channel();
        assertEq(D, 1 ether);
    }

    function test_signed_close_pays_balance_split() public {
        _open(1 ether);
        vm.prank(alice);
        ch.closeSigned(CID, bytes32(uint256(0x9911)), 0.3 ether, hex"");
        vm.warp(block.timestamp + TAU + 1);
        ch.finalize(CID);
        assertEq(ch.withdrawable(bob), 0.3 ether);
        assertEq(ch.withdrawable(alice), 0.7 ether);
        uint256 b0 = bob.balance;
        vm.prank(bob);
        ch.withdraw();
        assertEq(bob.balance - b0, 0.3 ether);
        vm.prank(alice);
        ch.withdraw();
        assertEq(address(ch).balance, 0);
    }

    function test_genesis_close_full_refund() public {
        _open(1 ether);
        vm.prank(alice);
        ch.closeGenesis(CID, bytes32(uint256(0x1)), hex"");
        vm.warp(block.timestamp + TAU + 1);
        ch.finalize(CID);
        assertEq(ch.withdrawable(alice), 1 ether);
    }

    // Alice closes stale (low balance); Bob challenges with a held message
    // whose nullifier collides with the exhibited one -> Alice forfeits all.
    function test_stale_close_challenged_forfeits_all() public {
        _open(1 ether);
        bytes32 exhibited = bytes32(uint256(0xBEEF));
        bytes32 root = ch.getLastRoot();
        vm.prank(alice);
        ch.closeSigned(CID, exhibited, 0.1 ether, hex""); // claims only 0.1
        vm.prank(bob);
        ch.challenge(CID, exhibited, bytes32(uint256(0xC1)), 0.2 ether, root, hex"");
        vm.warp(block.timestamp + TAU + 1);
        ch.finalize(CID);
        assertEq(ch.withdrawable(bob), 1 ether); // whole deposit
        assertEq(ch.withdrawable(alice), 0);
        vm.prank(bob);
        ch.withdraw();
        assertEq(address(ch).balance, 0);
    }

    function test_honest_close_challenge_wrong_nullifier_fails() public {
        _open(1 ether);
        bytes32 exhibited = bytes32(uint256(0xBEEF));
        bytes32 root = ch.getLastRoot();
        vm.prank(alice);
        ch.closeSigned(CID, exhibited, 0.5 ether, hex"");
        vm.prank(bob);
        vm.expectRevert("no collision");
        ch.challenge(CID, bytes32(uint256(0xDEAD)), bytes32(uint256(0xC1)), 1, root, hex"");
    }

    function test_unsigned_close_same_state_exception() public {
        _open(1 ether);
        bytes32 cX = bytes32(uint256(0xCC));
        bytes32 nX = bytes32(uint256(0x11));
        bytes32 nNext = bytes32(uint256(0x22));
        bytes32 root = ch.getLastRoot();
        vm.prank(alice);
        ch.closeUnsigned(CID, cX, nX, nNext, 0.2 ether, 0.2 ether, root, hex"");
        // Bob replaying the closed commitment itself is not valid evidence.
        vm.prank(bob);
        vm.expectRevert("same-state");
        ch.challenge(CID, nX, cX, 1, root, hex"");
    }

    function test_challenge_after_window_reverts() public {
        _open(1 ether);
        bytes32 exhibited = bytes32(uint256(0xBEEF));
        bytes32 root = ch.getLastRoot();
        vm.prank(alice);
        ch.closeSigned(CID, exhibited, 0.1 ether, hex"");
        vm.warp(block.timestamp + TAU + 1);
        vm.prank(bob);
        vm.expectRevert("window closed");
        ch.challenge(CID, exhibited, bytes32(uint256(0xC1)), 1, root, hex"");
    }

    function test_finalize_before_window_reverts() public {
        _open(1 ether);
        vm.prank(alice);
        ch.closeSigned(CID, bytes32(uint256(0x1)), 0.1 ether, hex"");
        vm.expectRevert("window open");
        ch.finalize(CID);
    }

    function test_timeout_forfeit_after_abs_deadline() public {
        _open(1 ether);
        vm.warp(block.timestamp + 90 days + 1);
        vm.prank(bob);
        ch.timeoutForfeit(CID);
        assertEq(ch.withdrawable(bob), 1 ether);
    }

    function test_timeout_forfeit_after_request_deadline() public {
        _open(1 ether);
        vm.prank(bob);
        ch.requestClose(CID);
        vm.warp(block.timestamp + 7 days + 1);
        vm.prank(bob);
        ch.timeoutForfeit(CID);
        assertEq(ch.withdrawable(bob), 1 ether);
    }

    function test_timeout_forfeit_blocked_if_closing() public {
        _open(1 ether);
        vm.prank(alice);
        ch.closeSigned(CID, bytes32(uint256(0x1)), 0.1 ether, hex"");
        vm.warp(block.timestamp + 90 days + 1);
        vm.prank(bob);
        vm.expectRevert("close pending");
        ch.timeoutForfeit(CID);
    }

    function test_only_alice_can_close() public {
        _open(1 ether);
        vm.prank(bob);
        vm.expectRevert("only alice");
        ch.closeSigned(CID, bytes32(uint256(0x1)), 0.1 ether, hex"");
    }

    function test_only_bob_can_challenge() public {
        _open(1 ether);
        bytes32 exhibited = bytes32(uint256(0xBEEF));
        bytes32 root = ch.getLastRoot();
        vm.prank(alice);
        ch.closeSigned(CID, exhibited, 0.1 ether, hex"");
        vm.prank(alice);
        vm.expectRevert("only bob");
        ch.challenge(CID, exhibited, bytes32(uint256(0xC1)), 1, root, hex"");
    }

    // Codex HIGH: a cid must never be reusable, even after settlement, or a
    // fresh deposit into it could be locked by the stale close record.
    function test_cid_not_reusable_after_settle() public {
        _open(1 ether);
        vm.prank(alice);
        ch.closeGenesis(CID, bytes32(uint256(0x1)), hex"");
        vm.warp(block.timestamp + TAU + 1);
        ch.finalize(CID);
        vm.prank(alice);
        vm.expectRevert("cid used");
        ch.open{value: 1 ether}(CID, bob, PKB, COPEN);
    }

    // Codex CRITICAL: an intra-epoch open must not move the accepted root and
    // invalidate a pending challenge proof.
    function test_open_does_not_move_epoch_root() public {
        bytes32 r0 = ch.getLastRoot();
        _open(1 ether);
        assertEq(ch.getLastRoot(), r0, "epoch root must be stable within the epoch");
        assertTrue(ch.rootAccepted(r0));
    }

    // Audit fix: an absurd duration would overflow `timestamp + duration` (uint64)
    // in finalize/timeout and brick the channel. The constructor rejects it.
    function test_constructor_rejects_absurd_duration() public {
        IVerifier v = IVerifier(new MockVerifier());
        vm.expectRevert("duration out of range");
        new ConfettiChannels(v, type(uint64).max, 90 days, 7 days, 1 days);
        vm.expectRevert("duration out of range");
        new ConfettiChannels(v, TAU, type(uint64).max, 7 days, 1 days);
    }

    // Audit fix: a role whose own address cannot receive ETH can still recover
    // its funds via withdrawTo(EOA); they are never permanently stranded.
    function test_withdrawTo_escapes_unreceivable_role() public {
        RevertOnReceive rc = new RevertOnReceive();
        address payable rcAddr = payable(address(rc));
        vm.prank(alice);
        ch.open{value: 1 ether}(CID, rcAddr, PKB, COPEN);
        vm.prank(alice);
        ch.closeSigned(CID, bytes32(uint256(0x9911)), 0.3 ether, hex"");
        vm.warp(block.timestamp + TAU + 1);
        ch.finalize(CID);
        assertEq(ch.withdrawable(rcAddr), 0.3 ether);

        // withdraw() to its own (reverting) address fails...
        vm.prank(rcAddr);
        vm.expectRevert("transfer failed");
        ch.withdraw();
        assertEq(ch.withdrawable(rcAddr), 0.3 ether, "balance preserved on failed withdraw");

        // ...but it can direct the payout to an EOA it controls.
        address payable eoa = payable(address(0xE0A));
        uint256 e0 = eoa.balance;
        vm.prank(rcAddr);
        ch.withdrawTo(eoa);
        assertEq(eoa.balance - e0, 0.3 ether);
        assertEq(ch.withdrawable(rcAddr), 0);
    }

    function _channel()
        internal
        view
        returns (uint256, bytes32, bytes32, address, address, uint64, uint64, bool)
    {
        return ch.channels(CID);
    }
}

/// A contract whose address cannot receive ETH — used to test that withdrawTo
/// lets such a role recover its funds anyway.
contract RevertOnReceive {
    receive() external payable {
        revert("cannot receive");
    }
}
