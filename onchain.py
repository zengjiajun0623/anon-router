"""Thin web3 client for the ConfettiChannels contract (M4b).

Binds the off-chain confetti channel to on-chain escrow: Alice's deposit sits
in the contract, and close/challenge/settlement are enforced by the chain.
The off-chain payment protocol (confetti/) is unchanged; this only moves
open/close/challenge/settle on chain.
"""
from __future__ import annotations

import json
import os

from web3 import Web3

ROOT = os.path.dirname(os.path.abspath(__file__))
ABI_PATH = os.path.join(ROOT, "contracts/out/ConfettiChannels.sol/ConfettiChannels.json")


def load_abi() -> list:
    with open(ABI_PATH) as f:
        return json.load(f)["abi"]


class Chain:
    def __init__(self, rpc: str, contract_addr: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc))
        self.contract = self.w3.eth.contract(
            address=Web3.to_checksum_address(contract_addr), abi=load_abi()
        )

    def _send(self, fn, sender_key: str, value: int = 0):
        acct = self.w3.eth.account.from_key(sender_key)
        tx = fn.build_transaction({
            "from": acct.address,
            "value": value,
            "nonce": self.w3.eth.get_transaction_count(acct.address),
            "gas": 800000,
            "gasPrice": self.w3.eth.gas_price,
        })
        signed = acct.sign_transaction(tx)
        h = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return self.w3.eth.wait_for_transaction_receipt(h)

    def last_root(self) -> bytes:
        return self.contract.functions.getLastRoot().call()

    def open(self, alice_key, cid: bytes, bob_addr: str, pk_B: bytes,
             c_open: bytes, deposit_wei: int):
        fn = self.contract.functions.open(
            cid, Web3.to_checksum_address(bob_addr), pk_B, c_open)
        return self._send(fn, alice_key, value=deposit_wei)

    def close_signed(self, alice_key, cid: bytes, n_next: bytes, bal_wei: int,
                     proof: bytes = b""):
        return self._send(
            self.contract.functions.closeSigned(cid, n_next, bal_wei, proof),
            alice_key)

    def close_genesis(self, alice_key, cid: bytes, n1: bytes, proof: bytes = b""):
        return self._send(
            self.contract.functions.closeGenesis(cid, n1, proof), alice_key)

    def challenge(self, bob_key, cid: bytes, n_m: bytes, c_m: bytes,
                  delta: int, root: bytes, proof: bytes = b""):
        return self._send(
            self.contract.functions.challenge(cid, n_m, c_m, delta, root, proof),
            bob_key)

    def finalize(self, any_key, cid: bytes):
        return self._send(self.contract.functions.finalize(cid), any_key)

    def withdraw(self, who_key):
        return self._send(self.contract.functions.withdraw(), who_key)

    def withdrawable(self, addr: str) -> int:
        return self.contract.functions.withdrawable(
            Web3.to_checksum_address(addr)).call()

    def channel(self, cid: bytes) -> dict:
        d, pkB, cOpen, alice, bob, openedAt, reqCloseAt, exists = (
            self.contract.functions.channels(cid).call())
        return {"deposit": d, "alice": alice, "bob": bob, "exists": exists}

    def contract_balance(self) -> int:
        return self.w3.eth.get_balance(self.contract.address)

    def balance(self, addr: str) -> int:
        return self.w3.eth.get_balance(Web3.to_checksum_address(addr))
