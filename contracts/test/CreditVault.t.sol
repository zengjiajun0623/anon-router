// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {CreditVault} from "../src/CreditVault.sol";

contract MockERC20 {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 approved = allowance[from][msg.sender];
        require(approved >= amount, "allowance");
        allowance[from][msg.sender] = approved - amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract CreditVaultTest is Test {
    event Deposited(bytes32 indexed keyHash, uint256 amount, address indexed from);
    event DepositedToken(bytes32 indexed keyHash, uint256 amount, address indexed from, address token);

    CreditVault internal vault;
    MockERC20 internal usdc;
    address internal alice = address(0xA11CE);
    bytes32 internal constant KEY_HASH = keccak256("api key");

    function setUp() public {
        usdc = new MockERC20();
        vault = new CreditVault(address(usdc));
        usdc.mint(alice, 10_000_000);
        vm.deal(alice, 1 ether);
    }

    function test_depositUSDC_pullsAmountAndEmits() public {
        uint256 amount = 2_500_000;
        vm.prank(alice);
        usdc.approve(address(vault), amount);

        vm.expectEmit(true, true, false, true, address(vault));
        emit DepositedToken(KEY_HASH, amount, alice, address(usdc));
        vm.prank(alice);
        vault.depositUSDC(KEY_HASH, amount);

        assertEq(usdc.balanceOf(alice), 7_500_000);
        assertEq(usdc.balanceOf(address(vault)), amount);
    }

    function test_depositUSDC_revertsWithoutApproval() public {
        vm.prank(alice);
        vm.expectRevert("allowance");
        vault.depositUSDC(KEY_HASH, 1_000_000);
    }

    function test_sweepToken_works() public {
        uint256 amount = 3_000_000;
        usdc.mint(address(vault), amount);

        vault.sweepToken(address(usdc), alice);

        assertEq(usdc.balanceOf(address(vault)), 0);
        assertEq(usdc.balanceOf(alice), 13_000_000);
    }

    function test_existingETHDepositPath() public {
        vm.expectEmit(true, true, false, true, address(vault));
        emit Deposited(KEY_HASH, 0.5 ether, alice);
        vm.prank(alice);
        vault.deposit{value: 0.5 ether}(KEY_HASH);

        assertEq(address(vault).balance, 0.5 ether);
    }
}
