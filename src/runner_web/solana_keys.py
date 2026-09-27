"""Program-derived addresses, computed locally so they cost no request."""

from __future__ import annotations

import hashlib

from runner_web.helius_discovery import _decode, _encode

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _on_curve(point: bytes) -> bool:
    """Whether 32 bytes decode to an ed25519 point; a PDA must not."""

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
    """A program-derived address, which no private key controls."""

    return not _on_curve(_decode(address, 44))


def find_program_address(seeds: list[bytes], program: str) -> str:
    program_id = _decode(program, 44)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join(seeds) + bytes([bump]) + program_id + b"ProgramDerivedAddress"
        ).digest()
        if not _on_curve(digest):
            return _encode(digest)
    raise ValueError("No program address for these seeds")


def bonding_curve(mint: str) -> str:
    return find_program_address([b"bonding-curve", _decode(mint, 44)], PUMP)


def token_account(owner: str, mint: str, token_program: str) -> str:
    """The owner's associated token account for a mint."""

    return find_program_address(
        [_decode(owner, 44), _decode(token_program, 44), _decode(mint, 44)],
        ASSOCIATED_TOKEN_PROGRAM,
    )
