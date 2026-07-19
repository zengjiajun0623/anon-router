"""Minimal pure-Python secp256k1 point arithmetic (no native deps).

Points are affine (x, y) int tuples; None is the point at infinity.
Plenty fast for a payment mint (a scalar mult is ~ms of bigint math).
"""
P = 2**256 - 2**32 - 977
N = int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)
G = (
    int("79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798", 16),
    int("483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8", 16),
)

Point = tuple[int, int] | None


def add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2:
        if (y1 + y2) % P == 0:
            return None
        m = (3 * x1 * x1) * pow(2 * y1, -1, P) % P
    else:
        m = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (m * m - x1 - x2) % P
    return (x3, (m * (x1 - x3) - y1) % P)


def mul(k: int, pt: Point) -> Point:
    k %= N
    result: Point = None
    addend = pt
    while k:
        if k & 1:
            result = add(result, addend)
        addend = add(addend, addend)
        k >>= 1
    return result


def neg(pt: Point) -> Point:
    if pt is None:
        return None
    return (pt[0], (P - pt[1]) % P)


def compress(pt: Point) -> bytes:
    if pt is None:
        raise ValueError("cannot compress point at infinity")
    x, y = pt
    return (b"\x02" if y % 2 == 0 else b"\x03") + x.to_bytes(32, "big")


def decompress(data: bytes) -> Point:
    if len(data) != 33 or data[0] not in (2, 3):
        raise ValueError("bad compressed point")
    x = int.from_bytes(data[1:], "big")
    if x >= P:
        raise ValueError("x out of range")
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if y * y % P != y_sq:
        raise ValueError("x not on curve")
    if (y % 2 == 0) != (data[0] == 2):
        y = P - y
    return (x, y)
