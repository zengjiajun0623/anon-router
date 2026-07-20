"""Operator settlement — pay an inference provider on-chain (ecash MVP, custodial).

In the ecash lane the router is custodial: users' ETH sits in the CreditVault as
the operator's float, and the operator settles with providers by paying out their
earned share. This tool simulates that payout on Sepolia: the vault owner sweeps
a provider's earnings (credits of inference served -> ETH) to the provider's
wallet, and shows the balance move on-chain.

Honest scope: this is the CUSTODIAL settlement mechanic (the operator pays; it
could withhold). Per-provider revenue attribution isn't tracked by the router yet
— `--credits` stands in for "inference this provider served". The NON-custodial
version, where a provider withdraws from escrow itself with no operator in the
loop, is the confetti channel lane (ConfettiChannels.close/withdraw) — roadmap.

  python provider_settle.py --credits 5000            # 20% router margin default
"""
import argparse
import json
import os
import sys

import httpx
from eth_account import Account
from web3 import Web3

ROUTER = os.environ.get("ANON_ROUTER_URL", "https://anon-router-production.up.railway.app")
DEFAULT_RPC = os.environ.get("ANON_RPC", "https://ethereum-sepolia-rpc.publicnode.com")


def _key(path: str) -> str:
    return json.load(open(path))["private_key"] if os.path.isfile(path) else path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--credits", type=int, default=5000,
                    help="credits of inference the provider served (their gross earnings)")
    ap.add_argument("--margin-bps", type=int, default=2000,
                    help="router margin withheld, in basis points (default 2000 = 20%%)")
    ap.add_argument("--owner-key", default=os.environ.get("ANON_DEPOSIT_KEY", ".sepolia-deployer.json"),
                    help="CreditVault owner (operator) key file/hex")
    ap.add_argument("--provider", default=".provider-wallet.json",
                    help="provider wallet file (address is paid)")
    ap.add_argument("--rpc", default=DEFAULT_RPC)
    args = ap.parse_args()

    cfg = httpx.get(f"{ROUTER}/config", timeout=20).json()
    vault, cpe = cfg["vault_address"], cfg["credits_per_eth"]
    if not vault:
        sys.exit("router has no vault configured")

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    owner = Account.from_key(_key(args.owner_key))
    provider = Web3.to_checksum_address(json.load(open(args.provider))["address"])

    gross_wei = args.credits * 10**18 // cpe
    payout_wei = gross_wei * (10_000 - args.margin_bps) // 10_000

    vault_addr = Web3.to_checksum_address(vault)
    vault_bal = w3.eth.get_balance(vault_addr)
    before = w3.eth.get_balance(provider)
    print(f"provider     {provider}")
    print(f"served       {args.credits} credits of inference")
    print(f"payout       {payout_wei/1e18:.6f} ETH  (after {args.margin_bps/100:.0f}% router margin)")
    print(f"vault        {vault_bal/1e18:.6f} ETH   provider before {before/1e18:.6f} ETH")
    if payout_wei == 0:
        sys.exit("payout rounds to zero — raise --credits")
    if payout_wei > vault_bal:
        sys.exit("payout exceeds vault balance")

    vault_c = w3.eth.contract(
        address=vault_addr,
        abi=[{"inputs": [{"name": "to", "type": "address"},
                         {"name": "amount", "type": "uint256"}],
              "name": "sweep", "outputs": [], "stateMutability": "nonpayable",
              "type": "function"}])
    tx = vault_c.functions.sweep(provider, payout_wei).build_transaction({
        "from": owner.address,
        "nonce": w3.eth.get_transaction_count(owner.address, "pending"),
        "gas": 80_000, "gasPrice": w3.eth.gas_price})
    txh = w3.eth.send_raw_transaction(owner.sign_transaction(tx).raw_transaction)
    h = txh.hex()
    print(f"settle tx    {h if h.startswith('0x') else '0x'+h} — waiting for confirmation…")
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=240)
    after = w3.eth.get_balance(provider)
    ok = rcpt.status == 1 and after > before
    print(f"provider after {after/1e18:.6f} ETH   (+{(after-before)/1e18:.6f})")
    print("PASS: provider received on-chain payout for inference served" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
