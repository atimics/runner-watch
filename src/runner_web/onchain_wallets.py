"""Saved Solana wallet history and PnL from observed trading costs."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from runner_web.db import connection
from runner_web.helius_discovery import _address, _decode, rpc_request
from runner_web.memecoin_chain_parser import SOL, USDC, parse_events, short_address
from runner_web.memecoins import _download
from runner_web.solana_keys import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from runner_web.wallet_registry import register_chain, wallet_id_for

PAGE_SIZE = 1_000
MAX_PAGES = 1
MAX_TOKEN_ACCOUNTS = 10_000
HOLDINGS_PER_PAGE = 100
PRICE_LIMIT = 30
REFRESH_SECONDS = 900
VERSION = "wallet-pnl-v1"
BOOK_VERSION = 1
MAX_POSITIONS = 20_000
SEED_PATH = Path(__file__).parent / "assets" / "kol-wallets.json"
QUOTE_UNITS = {SOL: "SOL", USDC: "USDC"}


def seed_wallets() -> list[dict[str, Any]]:
    seed = json.loads(SEED_PATH.read_text())
    return [
        {
            **row,
            "id": wallet_id_for(kind="solana", value=row["address"]),
            "href": "/wallet/" + wallet_id_for(kind="solana", value=row["address"]),
            "source_url": seed["source_url"],
            "label_date": seed["label_date"],
        }
        for row in seed["wallets"]
    ]


def wallet_label(address: str) -> dict[str, Any]:
    return next(
        (row for row in seed_wallets() if row["address"] == address),
        {"name": short_address(address), "emoji": "◉", "address": address},
    )


def wallet_catalog(query: str = "") -> dict[str, Any]:
    rows = seed_wallets()
    for row in rows:
        register_chain(row["address"])
    query = query.strip()
    rows = [
        row
        for row in rows
        if not query or query.casefold() in row["name"].casefold() or query in row["address"]
    ]
    with connection() as db:
        snapshots = {
            str(row["address"]): json.loads(row["payload_json"])
            for row in db.execute(
                "SELECT address,payload_json FROM onchain_wallet_snapshots "
                "WHERE payload_json IS NOT NULL ORDER BY updated_at DESC LIMIT 500"
            ).fetchall()
        }
    for row in rows:
        row["snapshot"] = snapshots.get(row["address"])
    return {"wallets": rows, "query": query, "label_date": "2025-01-26"}


def saved_wallet(address: str, *, at: datetime | None = None) -> dict[str, Any]:
    address = _address(address)
    current = at or datetime.now(UTC)
    with connection() as db:
        row = db.execute(
            "SELECT snapshots.payload_json,snapshots.error,snapshots.refresh_after,"
            "jobs.state_json,jobs.next_read_at FROM onchain_wallet_snapshots snapshots "
            "LEFT JOIN onchain_wallet_backfills jobs ON jobs.address=snapshots.address "
            "WHERE snapshots.address=?",
            (address,),
        ).fetchone()
    payload = json.loads(row["payload_json"]) if row and row["payload_json"] else {}
    updated = payload.get("updated_at")
    stale = bool(
        updated and (current - datetime.fromisoformat(updated)).total_seconds() >= REFRESH_SECONDS
    )
    state = json.loads(row["state_json"]) if row and row["state_json"] else {}
    return {
        **payload,
        "address": address,
        "label": wallet_label(address),
        "status": "stale" if stale else "ready" if payload else "pending",
        "error": row["error"] if row else None,
        "provider_ready": bool(os.getenv("HELIUS_API_KEY", "").strip()),
        "refresh_after": row["refresh_after"] if row else None,
        "backfill": {
            "status": "complete" if state.get("complete") else "loading" if state else "queued",
            "page_size": PAGE_SIZE,
            "next_read_at": row["next_read_at"] if row else None,
        },
    }


def _amount(value: Any) -> Decimal:
    raw, decimals = value["amount"], value["decimals"]
    if (
        not isinstance(raw, str)
        or not raw.isdigit()
        or len(raw) > 20
        or type(decimals) is not int
        or not 0 <= decimals <= 255
    ):
        raise ValueError("Invalid token balance")
    return Decimal(raw).scaleb(-decimals)


def holdings_page(snapshot: dict[str, Any], page: int = 1) -> dict[str, Any]:
    rows = snapshot.get("holdings", [])
    pages = max(1, (len(rows) + HOLDINGS_PER_PAGE - 1) // HOLDINGS_PER_PAGE)
    page = min(max(1, page), pages)
    start = (page - 1) * HOLDINGS_PER_PAGE
    return {
        **snapshot,
        "holdings": rows[start : start + HOLDINGS_PER_PAGE],
        "holdings_total": len(rows),
        "holdings_page": page,
        "holdings_pages": pages,
    }


def _balances(rows: Any, address: str) -> dict[str, Decimal]:
    if not isinstance(rows, list):
        raise ValueError("Token balance details are pending")
    totals: dict[str, Decimal] = {}
    for row in rows:
        # Missing owners can hide an incoming or outgoing balance.
        if not row.get("owner"):
            raise ValueError("Token ownership details are pending")
        if row["owner"] == address:
            mint = _address(row["mint"])
            totals[mint] = totals.get(mint, Decimal(0)) + _amount(row["uiTokenAmount"])
    return totals


def _number(value: Decimal | None) -> float | None:
    return float(value) if value is not None and value.is_finite() else None


def _price(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def calculate_pnl(
    address: str,
    entries: list[dict[str, Any]],
    holdings: dict[str, Decimal],
    quotes: dict[str, dict[str, Any]],
    *,
    checkpoint: dict[str, Any] | None = None,
    keep_checkpoint: bool = False,
    history_complete: bool = True,
) -> dict[str, Any]:
    """Average cost per quote currency, with unknown costs kept visible."""

    address = _address(address)
    book = checkpoint or {}
    if book and book.get("version") != BOOK_VERSION:
        raise ValueError("Wallet history needs a current buying-cost book")
    positions = {
        mint: {
            **row,
            "amount": Decimal(row["amount"]),
            "cost": Decimal(row["cost"]) if row["cost"] is not None else None,
        }
        for mint, row in book.get("positions", {}).items()
    }
    trades = list(reversed(book.get("trades", [])))
    receipts = list(reversed(book.get("receipts", [])))
    realized = {unit: Decimal(book.get("realized", {}).get(unit, "0")) for unit in ("SOL", "USDC")}
    closed = {unit: book.get("closed", {}).get(unit, 0) for unit in ("SOL", "USDC")}
    unknown_sales = book.get("unknown_sales", 0)
    unknown_by_unit = {
        unit: book.get("unknown_by_unit", {}).get(unit, 0) for unit in ("SOL", "USDC")
    }
    excluded = book.get("excluded", 0)
    fees = Decimal(book.get("fees", "0"))
    last_slot = book.get("last_slot", -1)
    tail = set(book.get("tail", []))
    oldest_at = book.get("oldest_at")
    history_hash = book.get("history_hash", "")
    unique = {}
    for entry in entries:
        try:
            signature = entry["transaction"]["signatures"][0]
            if len(_decode(signature, 88)) != 64:
                raise ValueError("Invalid transaction signature")
            slot, index = entry["slot"], entry.get("transactionIndex")
            if type(slot) is not int or slot < 0:
                raise ValueError("Invalid slot")
            if checkpoint and (slot < last_slot or signature in tail):
                continue
            # Preserve provider order within a slot if no index is provided.
            unique.setdefault(signature, (entry, len(unique), index))
        except (KeyError, TypeError, ValueError, IndexError):
            excluded += 1
    ordered = sorted(
        unique.values(),
        key=lambda row: (
            row[0]["slot"],
            row[2] if type(row[2]) is int else row[1] if keep_checkpoint else -row[1],
        ),
    )
    for entry, _, _ in ordered:
        signature = entry["transaction"]["signatures"][0]
        if entry["slot"] > last_slot:
            last_slot, tail = entry["slot"], set()
        tail.add(signature)
        meta = entry.get("meta") or {}
        try:
            keys = entry["transaction"]["message"]["accountKeys"]
            addresses = [key["pubkey"] if isinstance(key, dict) else key for key in keys]
            fee = meta["fee"]
            if type(fee) is not int or fee < 0:
                raise ValueError("Invalid fee")
            if addresses and addresses[0] == address:
                fees += Decimal(fee) / Decimal(10**9)
            receipts.append(
                {
                    "signature": signature,
                    "slot": entry["slot"],
                    "at": datetime.fromtimestamp(entry["blockTime"], UTC).isoformat(),
                    "href": "https://solscan.io/tx/" + signature,
                    "failed": meta.get("err") is not None,
                    "hash": hashlib.sha256(
                        json.dumps(entry, sort_keys=True, allow_nan=False).encode()
                    ).hexdigest(),
                }
            )
            oldest_at = oldest_at or receipts[-1]["at"]
            history_hash = hashlib.sha256(
                (history_hash + receipts[-1]["hash"]).encode()
            ).hexdigest()
            if meta.get("err") is not None:
                continue
            pre = _balances(meta.get("preTokenBalances"), address)
            post = _balances(meta.get("postTokenBalances"), address)
            changes = {
                mint: post.get(mint, Decimal(0)) - pre.get(mint, Decimal(0))
                for mint in pre.keys() | post.keys()
                if post.get(mint, Decimal(0)) != pre.get(mint, Decimal(0))
            }
            native = Decimal(0)
            if address in addresses:
                i = addresses.index(address)
                before, after = meta["preBalances"][i], meta["postBalances"][i]
                if type(before) is not int or type(after) is not int:
                    raise ValueError("Invalid native balance")
                native = Decimal(after - before) / Decimal(10**9)
            sol_delta = native + changes.get(SOL, Decimal(0))
            token_changes = {
                mint: delta for mint, delta in changes.items() if mint not in QUOTE_UNITS
            }
            swaps = {
                event["token_address"]
                for event in parse_events(entry)
                if event["kind"] == "swap" and event["wallet"] == address
            }
            quote_mint = USDC if changes.get(USDC) else SOL
            cash = changes.get(USDC, Decimal(0)) if quote_mint == USDC else sol_delta
            single_swap = len(token_changes) == 1 and bool(swaps)
            for mint, delta in token_changes.items():
                before = pre.get(mint, Decimal(0))
                position = positions.setdefault(
                    mint,
                    {
                        "amount": before,
                        "cost": Decimal(0) if before == 0 else None,
                        "unit": QUOTE_UNITS[quote_mint],
                    },
                )
                if position["amount"] != before:
                    position["cost"] = None
                position["amount"] = before
                unit = QUOTE_UNITS[quote_mint]
                is_trade = single_swap and mint in swaps and delta * cash < 0
                result = None
                if delta > 0:
                    if before == 0:
                        position.update(cost=Decimal(0), unit=unit)
                    if is_trade and position["unit"] == unit and position["cost"] is not None:
                        position["cost"] += -cash
                    else:
                        position["cost"] = None
                elif before > 0:
                    cost = (
                        position["cost"] * (-delta / before)
                        if position["cost"] is not None
                        else None
                    )
                    if is_trade:
                        if cost is not None and position["unit"] == unit:
                            result = cash - cost
                            realized[unit] += result
                            closed[unit] += 1
                        else:
                            unknown_sales += 1
                            unknown_by_unit[unit] += 1
                    if position["cost"] is not None:
                        position["cost"] -= cost
                position["amount"] = post.get(mint, Decimal(0))
                if position["amount"] == 0:
                    position["cost"] = Decimal(0)
                if is_trade:
                    trades.append(
                        {
                            "mint": mint,
                            "name": quotes.get(mint, {}).get("symbol") or short_address(mint),
                            "side": "Buy" if delta > 0 else "Sell",
                            "quantity": _number(abs(delta)),
                            "cash": _number(abs(cash)),
                            "unit": unit,
                            "realized": _number(result),
                            "cost_known": result is not None
                            if delta < 0
                            else position["cost"] is not None,
                            "signature": signature,
                            "href": "https://solscan.io/tx/" + signature,
                            "at": receipts[-1]["at"],
                        }
                    )
        except (KeyError, TypeError, ValueError, IndexError, InvalidOperation, AttributeError):
            excluded += 1
            # A skipped change can break a later cost basis.
            for position in positions.values():
                position["cost"] = None
    # Closed positions have no remaining buying cost to carry into the next page.
    positions = {mint: row for mint, row in positions.items() if row["amount"] > 0}
    if len(positions) > MAX_POSITIONS:
        raise ValueError("Wallet buying-cost book exceeds the saved position limit")
    rows = []
    unrealized = {"SOL": Decimal(0), "USDC": Decimal(0)}
    priced = {"SOL": 0, "USDC": 0}
    for mint, quantity in holdings.items():
        if quantity <= 0:
            continue
        position = positions.get(mint) if history_complete else None
        cost = position["cost"] if position and position["amount"] == quantity else None
        unit = position["unit"] if position else None
        price_usd = _price(quotes.get(mint, {}).get("price_usd"))
        quote_price = _price(quotes.get(SOL if unit == "SOL" else USDC, {}).get("price_usd"))
        value = quantity * price_usd if price_usd is not None else None
        gain = (
            value / quote_price - cost
            if (value is not None and quote_price is not None and cost is not None)
            else None
        )
        if mint in QUOTE_UNITS:
            cost, gain, unit = None, None, None
        if gain is not None:
            unrealized[unit] += gain
            priced[unit] += 1
        rows.append(
            {
                "mint": mint,
                "name": quotes.get(mint, {}).get("symbol") or short_address(mint),
                "quantity": _number(quantity),
                "value_usd": _number(value),
                "cost": _number(cost),
                "unit": unit,
                "unrealized": _number(gain),
                "href": "https://solscan.io/token/" + mint,
                "price_source": "GeckoTerminal" if price_usd else None,
            }
        )
    unknown_holdings = sum(row["cost"] is None for row in rows if row["mint"] not in QUOTE_UNITS)
    result = {
        "version": VERSION,
        "pnl": [
            {
                "unit": unit,
                "realized": _number(realized[unit])
                if closed[unit] or not unknown_by_unit[unit]
                else None,
                "closed_trades": closed[unit],
                "unknown_sales": unknown_by_unit[unit],
                "unrealized": _number(unrealized[unit]) if priced[unit] else None,
                "priced_positions": priced[unit],
            }
            for unit in ("SOL", "USDC")
        ],
        "holdings": sorted(rows, key=lambda row: row["value_usd"] or 0, reverse=True),
        "trades": list(reversed(trades))[:200],
        "receipts": list(reversed(receipts))[:200],
        "fees_sol": _number(fees),
        "unknown_sales": unknown_sales,
        "unknown_holdings": unknown_holdings,
        "excluded_transactions": excluded,
        "transactions": book.get("transactions", 0) + len(unique),
        "priced_holdings": sum(row["value_usd"] is not None for row in rows),
        "holdings_value_usd": _number(
            sum(
                (Decimal(str(row["value_usd"])) for row in rows if row["value_usd"] is not None),
                Decimal(0),
            )
        )
        if any(row["value_usd"] is not None for row in rows)
        else None,
        "oldest_at": oldest_at,
        "newest_at": receipts[-1]["at"] if receipts else None,
    }
    if keep_checkpoint:
        result["_checkpoint"] = {
            "version": BOOK_VERSION,
            "positions": {
                mint: {
                    **row,
                    "amount": str(row["amount"]),
                    "cost": str(row["cost"]) if row["cost"] is not None else None,
                }
                for mint, row in positions.items()
            },
            "realized": {unit: str(value) for unit, value in realized.items()},
            "closed": closed,
            "unknown_sales": unknown_sales,
            "unknown_by_unit": unknown_by_unit,
            "excluded": excluded,
            "fees": str(fees),
            "last_slot": last_slot,
            "tail": sorted(tail),
            "oldest_at": oldest_at,
            "history_hash": history_hash,
            "transactions": result["transactions"],
            "trades": result["trades"],
            "receipts": result["receipts"],
        }
    return result


def _rpc(call: Callable[..., Any], method: str, params: list[Any], credits: int) -> Any:
    reply = call({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, credits=credits)
    if not isinstance(reply, dict) or reply.get("error") or "result" not in reply:
        raise ValueError("Chain data is temporarily unavailable")
    return reply["result"]


def wallet_rpc(body: dict[str, Any], *, credits: int) -> dict[str, Any]:
    from runner_web.memecoin_evidence import release_wallet_credits

    charged_at = datetime.now(UTC)
    reply = rpc_request(body, credits=credits, lane="wallet", at=charged_at)
    if body["method"] == "getTransactionsForAddress":
        rows = reply.get("result", {}).get("data")
        if isinstance(rows, list) and len(rows) <= PAGE_SIZE:
            actual = max(10, ((len(rows) + 99) // 100) * 10)
            release_wallet_credits(credits - actual, at=charged_at)
    return reply


def collect_wallet(
    address: str,
    *,
    at: datetime,
    rpc: Callable[..., Any] = wallet_rpc,
    download: Callable[..., bytes] = _download,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    address = _address(address)
    state = state or {}
    book = state.get("book")
    window_end = state.get("window_end") if not state.get("complete") else None
    window_end = window_end if window_end is not None else int(at.timestamp())
    cursor = state.get("cursor")
    start_slot = state.get("start_slot")
    if state.get("complete"):
        start_slot = book.get("last_slot") if book else None
    options = {
        "transactionDetails": "full",
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 1,
        "commitment": "finalized",
        "sortOrder": "asc",
        "limit": PAGE_SIZE,
        "filters": {
            "status": "any",
            "tokenAccounts": "balanceChanged",
            "blockTime": {"lte": window_end},
        },
    }
    if start_slot is not None and start_slot >= 0:
        options["filters"]["slot"] = {"gte": start_slot}
    if cursor:
        options["paginationToken"] = cursor
    # History and the three current balance reads are independent.
    with ThreadPoolExecutor(max_workers=4) as pool:
        history_read = pool.submit(_rpc, rpc, "getTransactionsForAddress", [address, options], 100)
        balance_read = pool.submit(
            _rpc, rpc, "getBalance", [address, {"commitment": "finalized"}], 1
        )
        account_reads = [
            pool.submit(
                _rpc,
                rpc,
                "getTokenAccountsByOwner",
                [
                    address,
                    {"programId": program},
                    {"encoding": "jsonParsed", "commitment": "finalized"},
                ],
                1,
            )
            for program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)
        ]
        result = history_read.result()
        balance = balance_read.result()
        account_results = [read.result() for read in account_reads]
    entries = result.get("data")
    next_cursor = result.get("paginationToken")
    if not isinstance(entries, list) or len(entries) > PAGE_SIZE:
        raise ValueError("Chain history is temporarily unavailable")
    if next_cursor is not None and (
        not isinstance(next_cursor, str) or len(next_cursor) > 128 or next_cursor == cursor
    ):
        raise ValueError("Chain history is temporarily unavailable")
    for entry in entries:
        if (
            type(entry.get("blockTime")) is not int
            or entry["blockTime"] > window_end
            or type(entry.get("slot")) is not int
            or (start_slot is not None and entry["slot"] < start_slot)
        ):
            raise ValueError("Chain history is outside the requested window")
    if type(balance.get("value")) is not int or balance["value"] < 0:
        raise ValueError("Wallet balance is temporarily unavailable")
    quantities: dict[str, Decimal] = {}
    for result in account_results:
        accounts = result.get("value")
        if not isinstance(accounts, list) or len(accounts) > MAX_TOKEN_ACCOUNTS:
            raise ValueError("Wallet holdings exceed the current view limit")
        for account in accounts:
            info = account["account"]["data"]["parsed"]["info"]
            mint = _address(info["mint"])
            quantity = _amount(info["tokenAmount"])
            quantities[mint] = quantities.get(mint, Decimal(0)) + quantity
    quantities = {mint: value for mint, value in quantities.items() if value > 0}
    # Prices cover a bounded set; each missing mark remains pending.
    recent = []
    for entry in entries:
        try:
            for token in entry["meta"]["postTokenBalances"]:
                if token.get("owner") == address and token.get("mint") in quantities:
                    recent.append(token["mint"])
        except (KeyError, TypeError, AttributeError):
            continue
    known = [
        mint
        for mint, row in (book or {}).get("positions", {}).items()
        if row["cost"] is not None and mint in quantities
    ]
    price_mints = list(dict.fromkeys([*known, *recent, *quantities]))
    price_mints = [mint for mint in price_mints if mint not in QUOTE_UNITS]
    wanted = [SOL, USDC, *price_mints[: PRICE_LIMIT - 2]]
    quotes = {}
    try:
        url = "https://api.geckoterminal.com/api/v2/networks/solana/tokens/multi/"
        payload = json.loads(download(url + ",".join(dict.fromkeys(wanted)), 10.0))
        for token in payload["data"]:
            mint = token["id"].removeprefix("solana_")
            if mint in wanted:
                quotes[mint] = token["attributes"]
    except (ValueError, KeyError, TypeError, OSError):
        pass
    complete = not next_cursor
    payload = calculate_pnl(
        address,
        entries,
        quantities,
        quotes,
        checkpoint=book,
        keep_checkpoint=True,
        history_complete=complete,
    )
    checkpoint = payload.pop("_checkpoint")
    return {
        **payload,
        "address": address,
        "updated_at": at.isoformat(),
        "balance_sol": balance["value"] / 10**9,
        "has_more": not complete,
        "commitment": "finalized",
        "source": "Helius",
        "history_limit": MAX_PAGES * PAGE_SIZE,
        "history_hash": checkpoint["history_hash"],
        "_state": {
            "book": checkpoint,
            "window_end": window_end,
            "start_slot": start_slot,
            "cursor": next_cursor,
            "complete": complete,
        },
    }


def refresh_wallet(
    address: str,
    *,
    at: datetime | None = None,
    rpc: Callable[..., Any] = wallet_rpc,
    download: Callable[..., bytes] = _download,
) -> dict[str, Any]:
    """Advance the saved wallet history under one durable claim."""
    from runner_web.wallet_backfill import advance_wallet

    return advance_wallet(
        address,
        at=at,
        rpc=rpc,
        download=download,
        collect=collect_wallet,
    )
