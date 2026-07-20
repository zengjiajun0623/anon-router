"""M4b end-to-end: deposits leave custody, settlement enforced on-chain.

Runs against a local Anvil with ConfettiChannels + MockVerifier deployed.
Two channels demonstrate the two outcomes the trust model guarantees:

  1. HONEST: Alice deposits, makes off-chain payments (real confetti protocol),
     closes on-chain at her true balance, both parties get paid the split.
  2. FRAUD: Alice closes on a stale (cheaper) state; Bob challenges with a held
     message whose nullifier collides with the exhibited one; Alice forfeits
     the entire deposit.

Off-chain payments use the confetti library exactly as in M4a; only
open/close/challenge/finalize are on-chain here.
"""
import sys

from web3 import Web3

from confetti import Contract as PyReferee, Payer, Recipient
from onchain import Chain

RPC = "http://127.0.0.1:8545"
# Anvil well-known dev keys (public throwaways, guard no real value).
DEPLOYER = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ALICE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
BOB_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"


def eth(w):
    return f"{w / 1e18:.4f} ETH"


def warp(w3, seconds):
    w3.provider.make_request("evm_increaseTime", [seconds])
    w3.provider.make_request("evm_mine", [])


def main(addr):
    chain = Chain(RPC, addr)
    w3 = chain.w3
    alice_addr = w3.eth.account.from_key(ALICE_KEY).address
    bob_addr = w3.eth.account.from_key(BOB_KEY).address

    # The router (Bob) and an off-chain referee for countersigning (M4a).
    referee = PyReferee(tau=60)
    router = Recipient(height=6)

    print("=" * 66)
    print("CHANNEL 1 — HONEST close")
    print("=" * 66)
    payer = Payer.open_on(referee, D=10**18, pk_B=router.pk_B)  # D = 1 ETH in wei
    bob_before = chain.withdrawable(bob_addr)
    alice_before = chain.withdrawable(alice_addr)
    chain.open(ALICE_KEY, payer.cid, bob_addr, router.pk_B, payer.C_open, deposit_wei=10**18)
    print(f"  Alice opens, deposits 1 ETH -> contract holds {eth(chain.contract_balance())}")

    for i, delta in enumerate([2 * 10**17, 10**17, 15 * 10**16], 1):  # 0.2, 0.1, 0.15 ETH
        payer.pay(referee, router, delta)
        print(f"  off-chain payment {i}: +{eth(delta)}  (channel balance {eth(payer.tip.bal)})")

    tip = payer.tip
    chain.close_signed(ALICE_KEY, payer.cid, tip.N_next, tip.bal)
    print(f"  Alice closes on-chain at her true balance {eth(tip.bal)}")
    warp(w3, 61)
    chain.finalize(DEPLOYER, payer.cid)  # anyone can finalize after the window
    # Pull-payments: settlement credits a ledger; parties withdraw themselves.
    assert chain.withdrawable(bob_addr) - bob_before == tip.bal, "Bob owed exactly his earned balance"
    assert chain.withdrawable(alice_addr) - alice_before == 10**18 - tip.bal, "Alice owed the remainder"
    chain.withdraw(BOB_KEY)
    print(f"  finalized -> Bob withdraws {eth(tip.bal)}, Alice owed +{eth(10**18 - tip.bal)} (refund)")
    print("  OK: Bob got exactly what he earned; the rest is Alice's to withdraw.")

    print()
    print("=" * 66)
    print("CHANNEL 2 — FRAUD: stale close is challenged, Alice forfeits")
    print("=" * 66)
    payer2 = Payer.open_on(referee, D=10**18, pk_B=router.pk_B)
    chain.open(ALICE_KEY, payer2.cid, bob_addr, router.pk_B, payer2.C_open, deposit_wei=10**18)
    print(f"  Alice opens, deposits 1 ETH")
    state1 = _clone(payer2.pay(referee, router, 10**17))   # 0.1 ETH, Bob holds m1
    payer2.pay(referee, router, 5 * 10**17)                # 0.6 ETH true balance
    print(f"  two off-chain payments; true channel balance {eth(payer2.tip.bal)}")
    # Alice cheats: closes on the stale state 1 (claims only 0.1 ETH).
    chain.close_signed(ALICE_KEY, payer2.cid, state1.N_next, state1.bal)
    print(f"  Alice CHEATS: closes at stale balance {eth(state1.bal)} (not {eth(payer2.tip.bal)})")
    # Bob challenges with m2, whose revealed nullifier == exhibited N (state1.N_next).
    # The proof cites a chain-accepted epoch root (off-chain and on-chain
    # registries reconcile when the real verifier lands; see the design doc).
    m2 = router.inbox[-1]
    chain.challenge(BOB_KEY, payer2.cid, m2.N_i, m2.C_i, m2.delta, chain.last_root())
    print(f"  Bob challenges with a held message colliding on the exhibited nullifier")
    warp(w3, 61)
    bob_before = chain.withdrawable(bob_addr)      # bob's credit before this settle
    alice_before = chain.withdrawable(alice_addr)  # alice's channel-1 refund, untouched
    chain.finalize(DEPLOYER, payer2.cid)  # third party finalizes
    bob_gain = chain.withdrawable(bob_addr) - bob_before
    alice_gain = chain.withdrawable(alice_addr) - alice_before
    print(f"  finalized -> Bob +{eth(bob_gain)} (whole deposit); Alice +{eth(alice_gain)} from this channel")
    assert bob_gain == 10**18, "challenged close must award the entire deposit to Bob"
    assert alice_gain == 0, "cheating Alice forfeits everything from this channel"
    print("  OK: fraud caught on-chain, Alice lost the full deposit.")
    print()
    print("Both outcomes enforced by the contract. The router never had custody.")


def _clone(state):
    from confetti.channel import SignedState
    return SignedState(state.index, state.bal, state.C, state.r,
                       state.N_next, state.sigma, state.N_reveal)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else open("/tmp/confetti_addr.txt").read().strip())
