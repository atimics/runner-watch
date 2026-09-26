"""Protocol-specific Solana events with instruction-level source receipts."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from runner_web.helius_discovery import PUMP_SWAP, _address, _decode, _encode

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
PROGRAMS = {"pumpswap": PUMP_SWAP, "pump": PUMP, "raydium_cpmm": RAYDIUM}
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
CREATE = bytes([233, 146, 209, 142, 207, 104, 64, 188])
PUMP_CREATE = bytes([24, 30, 200, 40, 5, 28, 7, 119])
PUMP_CREATE_V2 = bytes([214, 144, 76, 236, 95, 139, 49, 180])
INITIALIZE = bytes([175, 175, 109, 31, 13, 152, 155, 237])
INITIALIZE_PERMISSION = bytes([63, 55, 254, 65, 49, 178, 89, 121])
BUY = bytes([102, 6, 61, 18, 1, 218, 235, 234])
BUY_QUOTE = bytes([198, 46, 21, 82, 180, 217, 232, 112])
SELL = bytes([51, 230, 133, 164, 1, 127, 131, 173])
WITHDRAW = bytes([183, 18, 70, 156, 148, 109, 161, 34])
SWAP_IN = bytes([143, 190, 90, 218, 196, 30, 51, 222])
SWAP_OUT = bytes([55, 217, 98, 86, 163, 74, 180, 173])


def short_address(address: str) -> str:
    """Head and tail of a contract address.

    Pump mints share a ground "pump" suffix and prefixes are cheap to grind, so
    six characters on each side keep look-alike mints apart in a list.
    """

    return address if len(address) <= 13 else address[:6] + "…" + address[-6:]


_BASE58 = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
# Letters that render as blank space: Hangul fillers and the Braille blank.
_INVISIBLE = frozenset("\u115f\u1160\u3164\uffa0\u2800")
# A pasted short form: base58 head, an ellipsis, base58 tail.
_PASTED_ADDRESS = re.compile(
    r"([1-9A-HJ-NP-Za-km-z]{3,})(?:\u2026|\.\.\.)([1-9A-HJ-NP-Za-km-z]{3,})"
)


def clean_claim(text: str, limit: int) -> str:
    """Creator-chosen launch text, reduced to plain visible characters.

    NFKC folds fullwidth and mathematical look-alike letters to plain ones.
    Control, format (bidi overrides, zero-width) and unassigned code points
    are dropped so the text cannot reorder or hide itself next to an
    address, as are blank-rendering letters and stacked combining marks.
    """

    kept = []
    marks = 0
    for char in unicodedata.normalize("NFKC", text):
        category = unicodedata.category(char)
        if category.startswith("Z"):
            kept.append(" ")
            marks = 0
        elif category.startswith("C") or char in _INVISIBLE or char == "\ufffd":
            continue
        elif category in ("Mn", "Me"):
            # One mark per letter covers real accents; more is a Zalgo stack.
            if marks == 0:
                kept.append(char)
            marks += 1
        else:
            kept.append(char)
            marks = 0
    return " ".join("".join(kept).split())[:limit]


def claim_text(raw: bytes, limit: int) -> str:
    return clean_claim(raw.decode("utf-8", errors="replace"), limit)


def looks_like_address(text: str) -> bool:
    """Text that could pass for a contract address: a short form such as
    `DezXAZ…pPB263`, or a base58 run as long as an address."""

    if _PASTED_ADDRESS.search(text):
        return True
    run = 0
    for char in text:
        run = run + 1 if char in _BASE58 else 0
        if run >= 32:
            return True
    return False


def display_claim(coin: dict[str, Any]) -> str | None:
    """The creator-set name to show beneath an address, or None. A claim
    that looks like an address is never shown: it could impersonate the
    coin it names."""

    claimed = clean_claim(str(coin.get("claimed_symbol") or coin.get("claimed_name") or ""), 64)
    if not claimed or looks_like_address(claimed):
        return None
    return claimed


def coin_search_rank(query: str, coin: dict[str, Any]) -> int | None:
    """How a search query matches a coin, best first, or None.

    0: the exact address or coin id. 1: the shown short form, a pasted
    head…tail, or an address prefix or suffix of at least four characters,
    case-sensitive as Solana addresses are. 2: the creator-set name. A query
    that looks like an address never falls back to names, so a coin cannot
    win an address search by copying the address into its name.
    """

    query = query.strip()
    address = str(coin.get("token_address") or "")
    if not query:
        return None
    if query == address or query.casefold() == str(coin.get("id") or ""):
        return 0
    # A full address typed in another case can only be this coin.
    if address and len(query) >= 32 and query.casefold() == address.casefold():
        return 0
    if not address:
        # Listings without an address are curated, not creator-set.
        text = f"{coin.get('symbol') or ''} {coin.get('name') or ''}".casefold()
        return 2 if query.casefold() in text else None
    if query == short_address(address):
        return 1
    pasted = _PASTED_ADDRESS.fullmatch(query)
    if pasted:
        head, tail = pasted.groups()
        return 1 if address.startswith(head) and address.endswith(tail) else None
    if looks_like_address(query):
        return None
    if (
        len(query) >= 4
        and set(query) <= _BASE58
        and (address.startswith(query) or address.endswith(query))
    ):
        return 1
    for key in ("claimed_symbol", "claimed_name"):
        claimed = clean_claim(str(coin.get(key) or ""), 64)
        if claimed and not looks_like_address(claimed) and query.casefold() in claimed.casefold():
            return 2
    return None


def balance_change(meta: dict[str, Any], wallet: str, mint: str) -> Decimal | None:
    totals = []
    found = False
    for field in ("preTokenBalances", "postTokenBalances"):
        total = Decimal(0)
        balances = meta.get(field)
        if not isinstance(balances, list):
            return None
        for balance in balances:
            if balance.get("mint") != mint:
                continue
            if not balance.get("owner"):
                return None
            if balance["owner"] != wallet:
                continue
            amount = balance["uiTokenAmount"]
            raw, decimals = amount["amount"], amount["decimals"]
            if (
                not isinstance(raw, str)
                or len(raw) > 20
                or not raw.isdigit()
                or type(decimals) is not int
                or not 0 <= decimals <= 255
            ):
                return None
            found = True
            total += Decimal(raw).scaleb(-decimals)
        totals.append(total)
    return totals[1] - totals[0] if found else None


def instruction_rows(entry: dict[str, Any]):
    for index, ix in enumerate(entry["transaction"]["message"]["instructions"]):
        yield str(index), ix
    for group in entry["meta"].get("innerInstructions") or []:
        for index, ix in enumerate(group["instructions"]):
            yield f"{group['index']}.{index}", ix


def valid_transactions(entries: list[Any], *, at: datetime) -> list[dict[str, Any]]:
    valid = []
    for entry in entries:
        try:
            if (
                entry["meta"]["err"] is not None
                or type(entry["slot"]) is not int
                or entry["slot"] < 0
                or type(entry["blockTime"]) is not int
                or not 0 <= at.timestamp() - entry["blockTime"] <= 30 * 86400
                or len(_decode(entry["transaction"]["signatures"][0], 88)) != 64
                or not isinstance(entry["transaction"]["message"]["instructions"], list)
            ):
                continue
            valid.append(entry)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return valid


def parse_events(entry: dict[str, Any]) -> list[dict[str, Any]]:
    signature = entry["transaction"]["signatures"][0]
    receipt = {
        "signature": signature,
        "slot": entry["slot"],
        "observed_at": datetime.fromtimestamp(entry["blockTime"], UTC).isoformat(),
        "source_url": f"https://solscan.io/tx/{signature}",
        "commitment": "finalized",
    }
    events = []
    for path, ix in instruction_rows(entry):
        try:
            program = ix.get("programId")
            fields = []
            if program == "11111111111111111111111111111111":
                parsed = ix.get("parsed", {})
                if parsed.get("type") == "transfer":
                    info = parsed["info"]
                    if type(info["lamports"]) is int and info["lamports"] > 0:
                        fields.append(
                            {
                                "kind": "sol_transfer",
                                "wallet": _address(info["destination"]),
                                "source_wallet": _address(info["source"]),
                                "lamports": str(info["lamports"]),
                            }
                        )
            elif program in PROGRAMS.values():
                data = _decode(ix.get("data"))
                discriminator = data[:8]
                accounts = ix["accounts"]
                if program == PUMP_SWAP and discriminator == CREATE and len(data) >= 58:
                    fields.append(
                        {
                            "kind": "pool_created",
                            "pool_address": _address(accounts[0]),
                            "wallet": _address(accounts[2]),
                            "token_address": _address(accounts[3]),
                            "quote_address": _address(accounts[4]),
                            "declared_creator": _encode(data[26:58]),
                        }
                    )
                elif program == PUMP and discriminator in (PUMP_CREATE, PUMP_CREATE_V2):
                    # Name, symbol and URI are Borsh strings the creator chose.
                    offset = 8
                    claims = []
                    for _ in range(3):
                        size = int.from_bytes(data[offset : offset + 4], "little")
                        if offset + 4 + size > len(data):
                            raise ValueError("Invalid launch instruction")
                        claims.append(data[offset + 4 : offset + 4 + size])
                        offset += 4 + size
                    if offset + 32 > len(data):
                        continue
                    fields.append(
                        {
                            "kind": "token_launch",
                            "token_address": _address(accounts[0]),
                            "wallet": _address(accounts[7 if discriminator == PUMP_CREATE else 5]),
                            "declared_creator": _encode(data[offset : offset + 32]),
                            "bonding_curve": _address(accounts[2]),
                            "claimed_name": claim_text(claims[0], 64),
                            "claimed_symbol": claim_text(claims[1], 16),
                        }
                    )
                elif program == RAYDIUM and discriminator in (INITIALIZE, INITIALIZE_PERMISSION):
                    shift = 1 if discriminator == INITIALIZE_PERMISSION else 0
                    token0, token1 = _address(accounts[4 + shift]), _address(accounts[5 + shift])
                    base, quote = (token1, token0) if token0 in (SOL, USDC) else (token0, token1)
                    fields.append(
                        {
                            "kind": "pool_created",
                            "pool_address": _address(accounts[3 + shift]),
                            "wallet": _address(accounts[shift]),
                            "token_address": base,
                            "quote_address": quote,
                            "token_0": token0,
                            "token_1": token1,
                        }
                    )
                elif program == PUMP_SWAP and discriminator in (BUY, BUY_QUOTE, SELL):
                    fields.append(
                        {
                            "kind": "swap",
                            "pool_address": _address(accounts[0]),
                            "wallet": _address(accounts[1]),
                            "token_address": _address(accounts[3]),
                            "direction": "sell" if discriminator == SELL else "buy",
                        }
                    )
                elif program == RAYDIUM and discriminator in (SWAP_IN, SWAP_OUT):
                    for mint_index, direction in ((10, "sell"), (11, "buy")):
                        fields.append(
                            {
                                "kind": "swap",
                                "pool_address": _address(accounts[3]),
                                "wallet": _address(accounts[0]),
                                "token_address": _address(accounts[mint_index]),
                                "direction": direction,
                            }
                        )
                elif discriminator == WITHDRAW and program in (PUMP_SWAP, RAYDIUM):
                    pool_index, wallet_index, mint_index = (
                        (0, 2, 3) if program == PUMP_SWAP else (2, 0, 10)
                    )
                    fields.append(
                        {
                            "kind": "liquidity_withdrawal",
                            "pool_address": _address(accounts[pool_index]),
                            "wallet": _address(accounts[wallet_index]),
                            "token_address": _address(accounts[mint_index]),
                            "lp_amount_raw": str(int.from_bytes(data[8:16], "little")),
                        }
                    )
            for field in fields:
                if field["kind"] in ("swap", "liquidity_withdrawal"):
                    delta = balance_change(entry["meta"], field["wallet"], field["token_address"])
                    if delta is None:
                        continue
                    if field["kind"] == "swap" and (
                        (field["direction"] == "sell" and delta >= 0)
                        or (field["direction"] == "buy" and delta <= 0)
                    ):
                        continue
                    if field["kind"] == "liquidity_withdrawal" and delta <= 0:
                        continue
                    field["net_token_amount"] = format(abs(delta), "f")
                    field["amount_basis"] = "wallet_net_change"
                event_id = f"{signature}:{path}:{field['kind']}:{field.get('token_address', '')}"
                events.append(
                    {
                        **receipt,
                        **field,
                        "event_id": event_id,
                        "program": program,
                        "instruction_path": path,
                    }
                )
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            continue
    return events
