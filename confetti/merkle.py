"""Merkle tree over channel records — the registry `root` that payment proofs
anchor to (Spec-v2 §2, G7). Epoch-quantized roots (§3) are handled by the
contract layer; here we just build/prove/verify membership.
"""
from __future__ import annotations

from .hashes import H


def _node(left: bytes, right: bytes) -> bytes:
    return H(b"reg-node", left, right)


class Registry:
    def __init__(self) -> None:
        self._leaves: list[bytes] = []

    def add(self, leaf: bytes) -> int:
        self._leaves.append(leaf)
        return len(self._leaves) - 1

    def _layers(self) -> list[list[bytes]]:
        if not self._leaves:
            return [[H(b"reg-empty")]]
        layers = [list(self._leaves)]
        while len(layers[-1]) > 1:
            cur = layers[-1]
            if len(cur) % 2:
                cur = cur + [cur[-1]]  # duplicate last for odd width
            layers.append([_node(cur[i], cur[i + 1]) for i in range(0, len(cur), 2)])
        return layers

    def root(self) -> bytes:
        return self._layers()[-1][0]

    def proof(self, index: int) -> list[bytes]:
        path, layers, idx = [], self._layers(), index
        for layer in layers[:-1]:
            cur = layer + ([layer[-1]] if len(layer) % 2 else [])
            path.append(cur[idx ^ 1])
            idx >>= 1
        return path


def verify_membership(root: bytes, leaf: bytes, index: int, path: list[bytes]) -> bool:
    node, idx = leaf, index
    for sib in path:
        node = _node(node, sib) if idx & 1 == 0 else _node(sib, node)
        idx >>= 1
    return node == root
