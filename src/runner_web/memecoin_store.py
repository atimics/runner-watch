from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from runner_web.db import connection
from runner_web.memecoin_replay_store import queue_replay

LOG = logging.getLogger(__name__)
HISTORY_DAYS = 7
MAX_HISTORY_POINTS = 250_000
MAX_DETAIL_HISTORY = 2016
# Prices read on the fast loop, kept apart from the slow snapshot so neither
# loop can overwrite the other: {"prices": {coin_id: {price, liquidity_usd, ...}}}.
FAST_PRICES_KEY = "memecoin_fast_prices"


def with_fast_price(
    row: dict[str, Any], fast: dict[str, Any] | None, collected_at: Any
) -> tuple[dict[str, Any], Any]:
    """A row with its newer fast-loop price laid over it, and the time that price was read.

    Only the price moves. Market cap and FDV follow it; the activity windows stay
    as the slow loop saw them. A row with no newer fast price comes back as given.
    """

    entry = ((fast or {}).get("prices") or {}).get(row.get("id"))
    if not isinstance(entry, dict):
        return row, collected_at
    try:
        seen = datetime.fromisoformat(entry["observed_at"])
        before = datetime.fromisoformat(row["observed_at"]) if row.get("observed_at") else None
        price = float(entry["price"])
        if price <= 0 or (before is not None and before >= seen):
            return row, collected_at
        merged = {**row, "price": price, "observed_at": entry["observed_at"]}
        merged["liquidity_usd"] = entry.get("liquidity_usd", row.get("liquidity_usd"))
        merged["time_basis"] = "chain_read"
        merged["source"] = "Solana (Helius)"
        old = row.get("price")
        if old and old > 0:
            for key in ("market_cap", "fully_diluted_valuation"):
                if row.get(key) is not None:
                    merged[key] = row[key] * price / old
        return merged, entry["observed_at"]
    except (KeyError, TypeError, ValueError):
        return row, collected_at


FAST_HISTORY_SECONDS = 300


def save_fast_quotes(
    quotes: list[dict[str, Any]], *, observed_at: datetime, every_tick: set[str] | None = None
) -> None:
    """History points for fast-loop prices, so charts and Call fills see them.

    Charts read the newest points, so a point a minute would shrink their span.
    A coin keeps the five-minute spacing unless a Call order is waiting on its
    next quote (`every_tick`).
    """

    stamp = observed_at.isoformat()
    spaced = (observed_at - timedelta(seconds=FAST_HISTORY_SECONDS)).isoformat()
    with connection() as database:
        for quote in quotes:
            asset = database.execute(
                "SELECT run_id FROM memecoin_assets WHERE coin_id=?", (quote["coin_id"],)
            ).fetchone()
            if asset is None:
                continue
            if (
                quote["coin_id"] not in (every_tick or set())
                and database.execute(
                    "SELECT 1 FROM memecoin_quote_history "
                    "WHERE coin_id=? AND observed_at>? LIMIT 1",
                    (quote["coin_id"], spaced),
                ).fetchone()
            ):
                continue
            database.execute(
                """
                INSERT INTO memecoin_quote_history(coin_id,observed_at,collected_at,price,run_id)
                VALUES(?,?,?,?,?) ON CONFLICT(coin_id,observed_at) DO NOTHING
                """,
                (quote["coin_id"], stamp, stamp, quote["price"], asset["run_id"]),
            )


def save_memecoin_snapshot(
    rows: list[dict[str, Any]], *, run_id: str, collected_at: datetime
) -> None:
    collected = collected_at.isoformat()
    oldest = (collected_at - timedelta(days=HISTORY_DAYS)).isoformat()
    started = last = time.monotonic()
    steps: dict[str, float] = {}

    def mark(name: str) -> None:
        nonlocal last
        now = time.monotonic()
        steps[name] = now - last
        last = now

    with connection() as database:
        for row in rows:
            database.execute(
                """
                INSERT INTO memecoin_assets(coin_id,quote_json,collected_at,run_id)
                VALUES(?,?,?,?) ON CONFLICT(coin_id) DO UPDATE SET
                    quote_json=excluded.quote_json,collected_at=excluded.collected_at,
                    run_id=excluded.run_id
                WHERE memecoin_assets.collected_at<=excluded.collected_at
                """,
                (row["id"], json.dumps(row, allow_nan=False), collected, run_id),
            )
            queue_replay(database, row, collected)
            observed = row.get("observed_at")
            if observed is None:
                continue
            observed_time = datetime.fromisoformat(observed)
            if not -60 <= (collected_at - observed_time).total_seconds() <= HISTORY_DAYS * 86400:
                continue
            early = row.get("early") or {}
            features = (
                json.dumps(
                    {
                        "version": early.get("version"),
                        **early.get("features", {}),
                        "score": early.get("score"),
                        "state": early.get("state"),
                    },
                    allow_nan=False,
                )
                if early
                else None
            )
            database.execute(
                """
                INSERT INTO memecoin_quote_history(
                    coin_id,observed_at,collected_at,price,run_id,features_json
                ) VALUES(?,?,?,?,?,?) ON CONFLICT(coin_id,observed_at) DO UPDATE SET
                    collected_at=excluded.collected_at,price=excluded.price,run_id=excluded.run_id,
                    features_json=excluded.features_json
                WHERE memecoin_quote_history.collected_at<=excluded.collected_at
                """,
                (row["id"], observed, collected, row["price"], run_id, features),
            )
        mark("rows")
        database.execute("DELETE FROM memecoin_quote_history WHERE observed_at<?", (oldest,))
        mark("prune_old")
        # Find the newest point past the cap, then delete everything older than it. This
        # reads the time index once; a NOT IN over every kept row did not finish in minutes.
        past_cap = database.execute(
            "SELECT observed_at FROM memecoin_quote_history "
            "ORDER BY observed_at DESC,coin_id LIMIT 1 OFFSET ?",
            (MAX_HISTORY_POINTS,),
        ).fetchone()
        if past_cap is not None:
            database.execute(
                "DELETE FROM memecoin_quote_history WHERE observed_at<=?",
                (past_cap["observed_at"],),
            )
        mark("prune_cap")
        for key, value in (
            (
                "memecoins_snapshot",
                {"rows": rows, "collected_at": collected, "run_id": run_id},
            ),
            ("memecoins_error", None),
        ):
            database.execute(
                """
                INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                WHERE worker_state.updated_at<=excluded.updated_at
                """,
                (key, json.dumps(value, allow_nan=False), collected),
            )
        mark("state")
    total = time.monotonic() - started
    if total > 30:
        detail = " ".join(f"{name}={value:.1f}" for name, value in steps.items())
        LOG.warning("memecoin_snapshot_save slow total=%.1f %s", total, detail)


def stored_memecoin(coin_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            "SELECT quote_json,collected_at,run_id FROM memecoin_assets WHERE coin_id=?",
            (coin_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "coin": json.loads(row["quote_json"]),
        "collected_at": row["collected_at"],
        "run_id": row["run_id"],
    }


SPARKLINE_POINTS = 48


def memecoin_sparklines(coin_ids: list[str], *, at: datetime) -> dict[str, list[dict[str, Any]]]:
    """Last 24 hours of saved prices for board rows, thinned to a sparkline's width."""
    ids = list(dict.fromkeys(coin_ids))[:50]
    if not ids:
        return {}
    with connection() as database:
        rows = database.execute(
            f"""
            SELECT coin_id,observed_at,price FROM memecoin_quote_history
            WHERE coin_id IN ({",".join("?" * len(ids))}) AND observed_at>=? AND observed_at<=?
            ORDER BY coin_id,observed_at
            """,
            (*ids, (at - timedelta(days=1)).isoformat(), (at + timedelta(seconds=60)).isoformat()),
        ).fetchall()
    series: dict[str, list[dict[str, Any]]] = {coin_id: [] for coin_id in ids}
    for row in rows:
        series[row["coin_id"]].append({"time": row["observed_at"], "price": row["price"]})
    for coin_id, points in series.items():
        if len(points) > SPARKLINE_POINTS:
            step = (len(points) - 1) / (SPARKLINE_POINTS - 1)
            series[coin_id] = [points[round(i * step)] for i in range(SPARKLINE_POINTS)]
    return series


def memecoin_history(coin_id: str, *, at: datetime, limit: int = 288) -> list[dict[str, Any]]:
    bounded_limit = max(1, min(int(limit), MAX_DETAIL_HISTORY))
    with connection() as database:
        rows = database.execute(
            """
            SELECT observed_at,collected_at,price FROM memecoin_quote_history
            WHERE coin_id=? AND observed_at>=? AND observed_at<=?
            ORDER BY observed_at DESC LIMIT ?
            """,
            (
                coin_id,
                (at - timedelta(days=HISTORY_DAYS)).isoformat(),
                (at + timedelta(seconds=60)).isoformat(),
                bounded_limit,
            ),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


# The early reading's states, in the stock chart's tones. An untagged stretch is
# "none", which the chart has no colour for, so the plain line shows through;
# "paused" would mean a stale quote on a coin, not "no tag".
STATE_TONES = {"setup": "setup", "running": "running", "extended": "extended", "avoid": "avoid"}


def memecoin_state_changes(coin_id: str, *, at: datetime, days: int = 7) -> list[dict[str, Any]]:
    """Where the coin's tag changed, so its chart can be drawn in the tag colours.

    Each saved quote keeps the tag it was given (features_json), so this is one
    query and no new storage. Quotes saved before tags were kept are skipped.
    """

    with connection() as database:
        rows = database.execute(
            """
            SELECT observed_at,features_json FROM memecoin_quote_history
            WHERE coin_id=? AND observed_at>=? AND observed_at<=? AND features_json IS NOT NULL
            ORDER BY observed_at
            """,
            (
                coin_id,
                (at - timedelta(days=days)).isoformat(),
                (at + timedelta(seconds=60)).isoformat(),
            ),
        ).fetchall()
    changes: list[dict[str, Any]] = []
    for row in rows:
        try:
            state = (json.loads(row["features_json"]) or {}).get("state")
        except (TypeError, ValueError):
            continue
        tone = STATE_TONES.get(str(state), "none")
        if changes and changes[-1]["tone"] == tone:
            continue
        changes.append({"time": str(row["observed_at"]), "tone": tone})
    return changes
