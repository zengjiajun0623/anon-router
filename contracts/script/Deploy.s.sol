// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script, console} from "forge-std/Script.sol";
import {ConfettiChannels} from "../src/ConfettiChannels.sol";
import {MockVerifier} from "../src/MockVerifier.sol";
import {IVerifier} from "../src/IVerifier.sol";

/// Deploy ConfettiChannels.
///
/// Env vars:
///   VERIFIER   - address of the real IVerifier. If unset, a MockVerifier is
///                deployed and the script REVERTS unless ALLOW_MOCK=1, so a
///                mock can never reach a real network by accident.
///   ALLOW_MOCK - "1" to permit the mock verifier (local/testnet demo only).
///   TAU,T_ABS,T_REQ,T_ROOT - timing constants in seconds (default to Spec §1:
///                7d / 90d / 7d / 1d).
///
/// Run (you hold the key; this repo never sees it):
///   forge script script/Deploy.s.sol --rpc-url $SEPOLIA_RPC \
///     --private-key $YOUR_KEY --broadcast
contract Deploy is Script {
    function run() external {
        uint64 tau = uint64(vm.envOr("TAU", uint256(7 days)));
        uint64 tAbs = uint64(vm.envOr("T_ABS", uint256(90 days)));
        uint64 tReq = uint64(vm.envOr("T_REQ", uint256(7 days)));
        uint64 tRoot = uint64(vm.envOr("T_ROOT", uint256(1 days)));

        address verifier = vm.envOr("VERIFIER", address(0));
        vm.startBroadcast();
        if (verifier == address(0)) {
            require(vm.envOr("ALLOW_MOCK", uint256(0)) == 1,
                "refusing to deploy MockVerifier: set VERIFIER, or ALLOW_MOCK=1 for a demo");
            verifier = address(new MockVerifier());
            console.log("WARNING: MockVerifier deployed (no security). Demo only.");
        }
        ConfettiChannels ch = new ConfettiChannels(
            IVerifier(verifier), tau, tAbs, tReq, tRoot);
        vm.stopBroadcast();
        console.log("Verifier:        ", verifier);
        console.log("ConfettiChannels:", address(ch));
    }
}
