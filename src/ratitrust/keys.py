"""Whether an address is program-derived, so no private key controls it."""

from __future__ import annotations

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _decode(address: str) -> bytes:
    number = 0
    for char in address:
        number = number * 58 + _B58.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\0" * (len(address) - len(address.lstrip("1"))) + raw


def _on_curve(point: bytes) -> bool:
    """Whether 32 bytes decode to an ed25519 point; a program address must not."""

    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
        if (x * x - x2) % _P:
            return False
    return True


def is_program_address(address: str) -> bool:
    raw = _decode(address)
    return len(raw) == 32 and not _on_curve(raw)
