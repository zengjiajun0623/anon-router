// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IVerifier} from "./IVerifier.sol";

/// @title ConfettiChannels — on-chain escrow + referee for the confetti
///        nullifier-chain payment channel (Spec-v2 §4-6).
/// @notice This is the M4b milestone: deposits leave the operator's custody and
///         sit in this contract; closes, the challenge game, and settlement are
///         enforced by the chain, not by the router's process. The router
///         (Bob) can no longer freeze or steal a payer's funds — his worst case
///         is receiving the whole deposit (which requires a valid challenge or
///         an AWOL payer), and the payer can always exit unilaterally.
///
///         Proof checking is delegated to `IVerifier`. On a real network that
///         MUST be the ZK STARK verifier; the local demo uses MockVerifier.
contract ConfettiChannels {
    // --- registry: incremental Merkle tree of channel records (Spec §2, G7) ---
    uint8 public constant TREE_DEPTH = 20;

    bytes32[TREE_DEPTH] private _filledSubtrees;
    bytes32[TREE_DEPTH] private _zeros;
    uint32 public nextLeafIndex;

    // Epoch-quantized roots (Spec §3, normative F-R1-3 privacy repair): proofs
    // cite the current epoch's root; verifiers accept only the current and
    // immediately previous epoch. This collapses the root to a public clock so
    // it fingerprints nothing, and bounds on-chain root retention.
    //
    // epochRoot[e] is WRITE-ONCE (snapshot at first touch of the epoch, taken
    // from the live root, i.e. reflecting all channels through epoch e-1). A
    // channel opened in epoch e is provable once epochRoot[e+1] is set. Making
    // it immutable within an epoch is what stops an attacker from opening a
    // throwaway channel to mutate the accepted root and invalidate a pending
    // challenge proof (a fund-loss front-run if the root moved per insert).
    mapping(uint256 => bytes32) public epochRoot;
    bytes32 private _liveRoot; // current incremental-tree root
    uint64 public immutable tRoot; // epoch length in seconds

    // --- channels & closes ---
    enum CloseMode { NONE, GENESIS, SIGNED, UNSIGNED }

    struct Channel {
        uint256 deposit; // D, in wei
        bytes32 pkB; // Bob's in-proof (WOTS/XMSS) public key
        bytes32 cOpen; // Com(c; r_open)
        address payable alice; // opener, receives the refund
        address payable bob; // recipient, receives earned balance
        uint64 openedAt;
        uint64 reqCloseAt; // >0 once Bob requests close (starts T_req)
        bool exists;
    }

    struct Close {
        CloseMode mode;
        uint256 bal; // claimed balance to Bob
        bytes32 exhibitA; // E member 1 (opened next-nullifier, or N_1)
        bytes32 exhibitB; // E member 2 (unsigned only: parent-reveal N_x)
        bytes32 cX; // published closing commitment (unsigned only)
        uint64 openedAt;
        bool challenged;
        bool finalized;
    }

    mapping(bytes16 => Channel) public channels;
    mapping(bytes16 => Close) public closes;
    mapping(bytes16 => bool) public usedCid; // cid is single-use forever (registry invariant)
    mapping(address => uint256) public withdrawable; // pull-payment ledger

    IVerifier public immutable verifier;
    uint64 public immutable tau; // challenge window
    uint64 public immutable tAbs; // absolute close deadline
    uint64 public immutable tReq; // close-on-request deadline

    uint256 private _locked = 1; // reentrancy guard

    event ChannelOpened(bytes16 indexed cid, uint32 leafIndex, uint256 deposit, bytes32 root);
    event CloseStarted(bytes16 indexed cid, uint8 mode, uint256 bal, uint64 windowEnds);
    event Challenged(bytes16 indexed cid);
    event Finalized(bytes16 indexed cid, uint256 toBob, uint256 toAlice);
    event Withdrawn(address indexed who, uint256 amount);

    modifier nonReentrant() {
        require(_locked == 1, "reentrant");
        _locked = 2;
        _;
        _locked = 1;
    }

    // A deadline is computed as `timestamp + duration` in uint64; an absurd
    // duration (near 2^64) would overflow that checked addition and revert
    // finalize/timeoutForfeit forever, permanently locking channel funds. Bound
    // every duration well below 2^64 so `timestamp + duration` can never overflow
    // for any realistic block.timestamp. ~2^40 s is ~34,000 years — a sanity
    // ceiling, not a real constraint.
    uint64 private constant MAX_DURATION = uint64(1) << 40;

    constructor(IVerifier _verifier, uint64 _tau, uint64 _tAbs, uint64 _tReq, uint64 _tRoot) {
        require(_tRoot > 0, "tRoot=0");
        require(
            _tau < MAX_DURATION && _tAbs < MAX_DURATION
                && _tReq < MAX_DURATION && _tRoot < MAX_DURATION,
            "duration out of range"
        );
        verifier = _verifier;
        tau = _tau;
        tAbs = _tAbs;
        tReq = _tReq;
        tRoot = _tRoot;
        for (uint8 i = 0; i < TREE_DEPTH - 1; i++) {
            _zeros[i + 1] = _hashPair(_zeros[i], _zeros[i]);
            _filledSubtrees[i] = _zeros[i];
        }
        _filledSubtrees[TREE_DEPTH - 1] = _zeros[TREE_DEPTH - 1];
        _liveRoot = _hashPair(_zeros[TREE_DEPTH - 1], _zeros[TREE_DEPTH - 1]);
        epochRoot[block.timestamp / _tRoot] = _liveRoot;
    }

    function currentEpoch() public view returns (uint256) {
        return block.timestamp / tRoot;
    }

    /// Accept only the current and immediately previous epoch's root (Spec §3).
    function rootAccepted(bytes32 r) public view returns (bool) {
        uint256 e = currentEpoch();
        if (r == bytes32(0)) return false;
        return r == epochRoot[e] || (e > 0 && r == epochRoot[e - 1]);
    }

    // --- open -----------------------------------------------------------
    function open(bytes16 cid, address payable bob, bytes32 pkB, bytes32 cOpen)
        external
        payable
        returns (uint32 leafIndex)
    {
        require(!usedCid[cid], "cid used"); // single-use forever; no reuse after settle
        require(msg.value > 0, "no deposit");
        require(bob != address(0), "bob=0");
        usedCid[cid] = true;
        _snapshotEpoch(); // freeze this epoch's accepted root before mutating the tree
        channels[cid] = Channel({
            deposit: msg.value,
            pkB: pkB,
            cOpen: cOpen,
            alice: payable(msg.sender),
            bob: bob,
            openedAt: uint64(block.timestamp),
            reqCloseAt: 0,
            exists: true
        });
        bytes32 leaf = keccak256(abi.encodePacked(cid, msg.value, pkB, cOpen));
        leafIndex = _insert(leaf);
        emit ChannelOpened(cid, leafIndex, msg.value, getLastRoot());
    }

    // --- Bob asks Alice to close (starts T_req) --------------------------
    function requestClose(bytes16 cid) external {
        Channel storage ch = channels[cid];
        require(ch.exists, "no channel");
        require(msg.sender == ch.bob, "only bob");
        require(ch.reqCloseAt == 0, "already requested");
        ch.reqCloseAt = uint64(block.timestamp);
    }

    // --- closes ---------------------------------------------------------
    function closeGenesis(bytes16 cid, bytes32 n1, bytes calldata proof) external {
        Channel storage ch = channels[cid];
        require(ch.exists, "no channel");
        require(msg.sender == ch.alice, "only alice");
        require(verifier.verifyGenesisClose(cid, ch.cOpen, n1, proof), "bad proof");
        _startClose(cid, CloseMode.GENESIS, 0, n1, bytes32(0), bytes32(0));
    }

    function closeSigned(bytes16 cid, bytes32 nNext, uint256 bal, bytes calldata proof)
        external
    {
        Channel storage ch = channels[cid];
        require(ch.exists, "no channel");
        require(msg.sender == ch.alice, "only alice");
        require(bal <= ch.deposit, "bal>D");
        require(
            verifier.verifySignedClose(cid, ch.deposit, ch.pkB, nNext, bal, proof),
            "bad proof"
        );
        _startClose(cid, CloseMode.SIGNED, bal, nNext, bytes32(0), bytes32(0));
    }

    function closeUnsigned(
        bytes16 cid,
        bytes32 cX,
        bytes32 nX,
        bytes32 nNext,
        uint256 bal,
        uint256 delta,
        bytes32 root,
        bytes calldata proof
    ) external {
        Channel storage ch = channels[cid];
        require(ch.exists, "no channel");
        require(msg.sender == ch.alice, "only alice");
        require(bal <= ch.deposit, "bal>D");
        require(rootAccepted(root), "unknown root");
        // bal, nNext and nX are PROOF-BOUND public inputs (constrained to C_x by
        // R_closeUnsigned) — not trusted caller values. Fixes the theft where a
        // valid proof was paired with a lie about the balance / exhibit set.
        require(
            verifier.verifyCloseUnsigned(cid, ch.deposit, root, delta, nX, cX, bal, nNext, proof),
            "bad proof"
        );
        _startClose(cid, CloseMode.UNSIGNED, bal, nNext, nX, cX);
    }

    function _startClose(
        bytes16 cid,
        CloseMode mode,
        uint256 bal,
        bytes32 exhibitA,
        bytes32 exhibitB,
        bytes32 cX
    ) private {
        require(closes[cid].mode == CloseMode.NONE, "already closing");
        uint64 nowT = uint64(block.timestamp);
        closes[cid] = Close({
            mode: mode,
            bal: bal,
            exhibitA: exhibitA,
            exhibitB: exhibitB,
            cX: cX,
            openedAt: nowT,
            challenged: false,
            finalized: false
        });
        emit CloseStarted(cid, uint8(mode), bal, nowT + tau);
    }

    // --- challenge (Spec §5) --------------------------------------------
    function challenge(
        bytes16 cid,
        bytes32 nM,
        bytes32 cM,
        uint256 delta,
        bytes32 root,
        bytes calldata proof
    ) external {
        Channel storage ch = channels[cid];
        Close storage cl = closes[cid];
        require(cl.mode != CloseMode.NONE, "not closing");
        require(!cl.challenged, "already challenged");
        require(block.timestamp <= cl.openedAt + tau, "window closed");
        require(msg.sender == ch.bob, "only bob");
        require(rootAccepted(root), "unknown root"); // (1) proof-validity anchor
        require(verifier.verifyPayment(delta, nM, cM, root, proof), "bad proof");
        // (2) same-state exception: only unsigned closes publish C_x.
        if (cl.mode == CloseMode.UNSIGNED) {
            require(cM != cl.cX, "same-state");
        }
        // (3) N_m collides with an exhibited nullifier of the close.
        require(nM == cl.exhibitA || (cl.exhibitB != bytes32(0) && nM == cl.exhibitB), "no collision");
        cl.challenged = true;
        emit Challenged(cid);
    }

    // --- settlement (Spec §6) -------------------------------------------
    function finalize(bytes16 cid) external nonReentrant {
        Channel storage ch = channels[cid];
        Close storage cl = closes[cid];
        require(cl.mode != CloseMode.NONE, "not closing");
        require(!cl.finalized, "finalized");
        require(block.timestamp > cl.openedAt + tau, "window open");
        cl.finalized = true;
        uint256 D = ch.deposit;
        uint256 toBob = cl.challenged ? D : cl.bal;
        uint256 toAlice = D - toBob;
        ch.exists = false;
        _credit(ch.bob, toBob);
        _credit(ch.alice, toAlice);
        emit Finalized(cid, toBob, toAlice);
    }

    /// Bob claims the whole deposit if Alice never closed past a deadline (§4).
    function timeoutForfeit(bytes16 cid) external nonReentrant {
        Channel storage ch = channels[cid];
        Close storage cl = closes[cid];
        require(ch.exists, "no channel");
        require(cl.mode == CloseMode.NONE, "close pending");
        require(msg.sender == ch.bob, "only bob");
        bool absPassed = block.timestamp > ch.openedAt + tAbs;
        bool reqPassed = ch.reqCloseAt != 0 && block.timestamp > ch.reqCloseAt + tReq;
        require(absPassed || reqPassed, "no deadline passed");
        uint256 D = ch.deposit;
        ch.exists = false;
        cl.finalized = true;
        cl.mode = CloseMode.SIGNED; // mark terminal so finalize can't re-run
        _credit(ch.bob, D);
        emit Finalized(cid, D, 0);
    }

    /// Pull-payment: settlement only credits a ledger; recipients withdraw
    /// themselves. A reverting recipient can no longer block the counterparty's
    /// payout (the atomic-push griefing Codex flagged), and finalize makes no
    /// external call, so there is nothing to re-enter.
    function _credit(address to, uint256 amount) private {
        if (amount != 0) withdrawable[to] += amount;
    }

    function withdraw() external nonReentrant {
        _withdrawTo(payable(msg.sender));
    }

    /// Withdraw the caller's earned balance to a chosen address. Escape hatch for
    /// a role whose OWN address cannot receive ETH (e.g. a contract that reverts
    /// on receive): its funds can then never be permanently stranded, since it
    /// can direct the payout to an EOA it controls.
    function withdrawTo(address payable to) external nonReentrant {
        require(to != address(0), "zero addr");
        _withdrawTo(to);
    }

    /// Pull the CALLER's earned balance (keyed by msg.sender) and send it to
    /// `to`. Checks-effects-interactions: the ledger slot is zeroed before the
    /// external call, and both entrypoints are nonReentrant.
    function _withdrawTo(address payable to) private {
        uint256 amount = withdrawable[msg.sender];
        require(amount > 0, "nothing to withdraw");
        withdrawable[msg.sender] = 0;
        (bool ok,) = to.call{value: amount}("");
        require(ok, "transfer failed");
        emit Withdrawn(msg.sender, amount);
    }

    // --- incremental Merkle tree ----------------------------------------
    function _insert(bytes32 leaf) private returns (uint32 index) {
        require(nextLeafIndex < uint32(2) ** TREE_DEPTH, "tree full");
        uint32 idx = nextLeafIndex;
        bytes32 cur = leaf;
        for (uint8 i = 0; i < TREE_DEPTH; i++) {
            if (idx & 1 == 0) {
                _filledSubtrees[i] = cur;
                cur = _hashPair(cur, _zeros[i]);
            } else {
                cur = _hashPair(_filledSubtrees[i], cur);
            }
            idx >>= 1;
        }
        _liveRoot = cur;
        index = nextLeafIndex;
        nextLeafIndex++;
    }

    /// Freeze the accepted root for the current epoch, once, before the tree is
    /// mutated. Write-once so intra-epoch opens cannot move an accepted root.
    function _snapshotEpoch() private {
        uint256 e = block.timestamp / tRoot;
        if (epochRoot[e] == bytes32(0)) epochRoot[e] = _liveRoot;
    }

    function _hashPair(bytes32 a, bytes32 b) private pure returns (bytes32) {
        return keccak256(abi.encodePacked(a, b));
    }

    function getLastRoot() public view returns (bytes32) {
        return epochRoot[currentEpoch()];
    }
}
