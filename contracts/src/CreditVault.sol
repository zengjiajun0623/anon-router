// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title CreditVault — the simple deposit front door.
/// @notice A buyer deposits ETH referencing their account (keyHash = hash of
///         their API key). The router watches `Deposited` events and credits
///         that account. This is the custodial "simpler than OpenRouter" lane:
///         deposit -> instant API key -> use it. The anonymous, trust-minimized
///         lane is ConfettiChannels; this one trades that for one-tap UX.
/// @dev    keyHash binds the on-chain payment to an off-chain account without
///         revealing the key. USDC support is the same with transferFrom.
contract CreditVault {
    address public owner;
    address public immutable usdc;

    event Deposited(bytes32 indexed keyHash, uint256 amount, address indexed from);
    event DepositedToken(bytes32 indexed keyHash, uint256 amount, address indexed from, address token);

    constructor(address usdc_) {
        owner = msg.sender;
        usdc = usdc_;
    }

    /// Deposit ETH crediting the account identified by keyHash.
    function deposit(bytes32 keyHash) external payable {
        require(msg.value > 0, "no value");
        require(keyHash != bytes32(0), "keyHash=0");
        emit Deposited(keyHash, msg.value, msg.sender);
    }

    /// Deposit USDC crediting the account identified by keyHash.
    function depositUSDC(bytes32 keyHash, uint256 amount) external {
        require(usdc != address(0), "USDC disabled");
        require(IERC20(usdc).transferFrom(msg.sender, address(this), amount), "transferFrom failed");
        emit DepositedToken(keyHash, amount, msg.sender, usdc);
    }

    /// Operator sweeps deposited funds (they back the credits sold).
    function sweep(address payable to, uint256 amount) external {
        require(msg.sender == owner, "only owner");
        (bool ok,) = to.call{value: amount}("");
        require(ok, "sweep failed");
    }

    /// Operator sweeps all deposited tokens (they back the credits sold).
    function sweepToken(address token, address to) external {
        require(msg.sender == owner, "only owner");
        IERC20 erc20 = IERC20(token);
        require(erc20.transfer(to, erc20.balanceOf(address(this))), "transfer failed");
    }
}
