"""Transition alerts: tell the room when a ratified name changes state.

Each pass compares a name's action tag and ratified flag with the last ones
kept in `transition_state`. A change that matters becomes a pending row in
`transition_events` with an interest score computed here, in code. Each turn the
best pending event inside its daily cap moves to the telegram outbox, one at a
time. The rules are written up in docs/telegram-channel.md.
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any

LOG = logging.getLogger(__name__)

EVENT_WEIGHTS = {
    "running": 100.0,
    "newly_ratified": 80.0,
    "setup": 60.0,
    "extended": 50.0,
    "lost_ratification": 45.0,
    "avoid": 40.0,
}
CALL_BONUS = 25.0
# A name last seen longer ago than this is a first sighting again, not a change.
STATE_MAX_AGE = timedelta(hours=24)
# Only rewrite an unchanged name's row this often.
STATE_REFRESH = timedelta(hours=1)
# Only snapshots this fresh count as "on the board now".
BOARD_WINDOW = timedelta(minutes=45)
STOCK_LIMIT = 400
MARKETS = ("stock", "memecoin")


def _int_env(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def daily_cap(market: str) -> int:
    if market == "stock":
        return _int_env("TELEGRAM_TRANSITIONS_PER_DAY_STOCK", 8)
    return _int_env("TELEGRAM_TRANSITIONS_PER_DAY_MEMECOIN", 4)


def max_age_minutes() -> int:
    return _int_env("TELEGRAM_TRANSITION_MAX_AGE_MINUTES", 180)


def normalize_tag(tag: Any) -> str:
    """The tag as a lowercase word; no tag and a quiet coin both read as watch."""

    value = str(tag or "").strip().lower()
    return "watch" if value in {"", "quiet"} else value


def classify(
    before_tag: str,
    before_ratified: bool | None,
    tag: str,
    ratified: bool | None,
    has_call: bool,
) -> tuple[str, str, str] | None:
    """The one event worth telling, as (event, from, to), or None.

    Ratification changes need a known state on both sides. Tag moves only speak
    for a ratified name or one with an active Call.
    """

    found: list[tuple[str, str, str]] = []
    if before_ratified is False and ratified is True:
        found.append(("newly_ratified", "unratified", "ratified"))
    if before_ratified is True and ratified is False:
        found.append(("lost_ratification", "ratified", "unratified"))
    before, now = normalize_tag(before_tag), normalize_tag(tag)
    if before != now and "paused" not in (before, now) and before != "avoid":
        allowed = ratified is True or has_call
        if now == "avoid":
            if allowed or before_ratified is True:
                found.append(("avoid", before, now))
        elif allowed:
            if (
                (now == "setup" and before == "watch")
                or (now == "running" and before in {"watch", "setup"})
                or (now == "extended" and before == "running")
            ):
                found.append((now, before, now))
    if not found:
        return None
    return max(found, key=lambda item: EVENT_WEIGHTS[item[0]])


def _unit(value: Any, top: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number <= 0:
        return 0.0
    return min(number, top) / top


def _log_unit(value: Any, floor: float, top: float) -> float:
    """0 at `floor`, 1 at `top`, on a log scale."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number) or number <= floor:
        return 0.0
    return min(1.0, math.log10(number / floor) / math.log10(top / floor))


def interest(event: str, facts: dict[str, Any], has_call: bool) -> float:
    """Interest in code: event weight, ratified share, scanner score, activity, Call."""

    score = EVENT_WEIGHTS[event]
    total = facts.get("total") or 0
    if total:
        score += 20.0 * min(1.0, max(0.0, (facts.get("met") or 0) / total))
    score += 20.0 * _unit(facts.get("score"), 100.0)
    if facts.get("market") == "stock":
        score += 20.0 * _unit(facts.get("relative_volume"), 10.0)
    else:
        score += 10.0 * _log_unit(facts.get("liquidity_usd"), 5_000.0, 1_000_000.0)
        score += 10.0 * _log_unit(facts.get("volume_24h"), 5_000.0, 5_000_000.0)
    if has_call:
        score += CALL_BONUS
    return round(score, 2)


def _stamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stock_observations(database: Any, at: datetime) -> dict[str, dict[str, Any]]:
    from runner_web.market_screens import state_tag
    from runner_web.stock_ratify import stock_ratifications

    cutoff = (at - BOARD_WINDOW).isoformat()
    rows = database.execute(
        """
        SELECT s.ticker,s.score,s.relative_volume,s.dollar_volume,s.trade_state,
               s.stage,s.rug_level,s.captured_at
        FROM scan_snapshots s
        JOIN (
            SELECT ticker,MAX(captured_at) AS seen FROM scan_snapshots
            WHERE captured_at>=? GROUP BY ticker
        ) latest ON latest.ticker=s.ticker AND latest.seen=s.captured_at
        ORDER BY s.score DESC LIMIT ?
        """,
        (cutoff, STOCK_LIMIT),
    ).fetchall()
    board: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        ticker = str(row["ticker"] or "").upper()
        if ticker and ticker not in board:
            board[ticker] = {**row, "ticker": ticker}
    if not board:
        return {}
    try:
        found = stock_ratifications(database, list(board.values()), at=at)
    except Exception:
        LOG.warning("Transition alerts could not read stock ratification", exc_info=True)
        found = {}
    observed = {}
    for ticker, row in board.items():
        result = found.get(ticker)
        observed[ticker] = {
            "market": "stock",
            "subject": ticker,
            "ticker": ticker,
            "tag": normalize_tag(state_tag(row)[1]),
            "ratified": bool(result.get("ratified")) if result else None,
            "met": (result or {}).get("met"),
            "total": (result or {}).get("total"),
            "score": row.get("score"),
            "relative_volume": row.get("relative_volume"),
        }
    return observed


def _coin_ratified(rated: dict[str, Any] | None) -> bool | None:
    """Ratified, not ratified, or unknown while a standard is unchecked and none failed.

    A failed chain read leaves standards unchecked; that is not news, so it must
    not read as a lost ratification.
    """

    if not rated:
        return None
    if rated.get("ratified"):
        return True
    standards = [item for item in rated.get("standards") or [] if isinstance(item, dict)]
    if any(item.get("applies", True) and item.get("met") is False for item in standards):
        return False
    return None if rated.get("unchecked") else False


def _memecoin_observations(database: Any, at: datetime) -> dict[str, dict[str, Any]]:
    cutoff = (at - BOARD_WINDOW).isoformat()
    observed = {}
    for raw in database.execute(
        "SELECT coin_id,quote_json FROM memecoin_assets WHERE collected_at>=?", (cutoff,)
    ).fetchall():
        try:
            quote = json.loads(raw["quote_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(quote, dict):
            continue
        mint = str(quote.get("token_address") or "").strip()
        if not mint:
            continue
        early = quote.get("early") if isinstance(quote.get("early"), dict) else {}
        rated = quote.get("ratification") if isinstance(quote.get("ratification"), dict) else None
        observed[mint] = {
            "market": "memecoin",
            "subject": mint,
            "coin_id": str(raw["coin_id"]),
            "tag": normalize_tag(early.get("state")),
            "ratified": _coin_ratified(rated),
            "met": (rated or {}).get("met"),
            "total": (rated or {}).get("total"),
            "score": early.get("score"),
            "liquidity_usd": quote.get("liquidity_usd"),
            "volume_24h": quote.get("volume_24h"),
        }
    return observed


def _active_calls(database: Any) -> tuple[set[str], set[str]]:
    tickers = {
        str(row["ticker"]).upper()
        for row in database.execute(
            "SELECT DISTINCT ticker FROM community_calls WHERE status='active'"
        ).fetchall()
    }
    coins = {
        str(row["coin_id"])
        for row in database.execute(
            "SELECT DISTINCT coin_id FROM memecoin_calls WHERE status='active'"
        ).fetchall()
    }
    return tickers, coins


def detect(database: Any, market: str, observed: dict[str, dict[str, Any]], at: datetime) -> int:
    """Compare each name with its kept state, record changes; returns events stored."""

    if not observed:
        return 0
    tickers, coins = _active_calls(database)
    subjects = list(observed)
    kept: dict[str, Any] = {}
    for offset in range(0, len(subjects), 500):
        chunk = subjects[offset : offset + 500]
        marks = ",".join("?" for _ in chunk)
        for row in database.execute(
            f"SELECT subject,tag,ratified,updated_at FROM transition_state "
            f"WHERE market=? AND subject IN ({marks})",
            (market, *chunk),
        ).fetchall():
            kept[row["subject"]] = row
    stored = 0
    day = at.date().isoformat()
    for subject, facts in observed.items():
        before = kept.get(subject)
        seen = _stamp(before["updated_at"]) if before else None
        ratified = facts["ratified"]
        known_ratified = (
            ratified if ratified is not None else (None if before is None else before["ratified"])
        )
        if before is not None and seen is not None and at - seen <= STATE_MAX_AGE:
            before_ratified = None if before["ratified"] is None else bool(before["ratified"])
            has_call = subject in tickers if market == "stock" else facts.get("coin_id") in coins
            found = classify(before["tag"], before_ratified, facts["tag"], ratified, has_call)
            if found:
                event, from_tag, to_tag = found
                key = f"transition:{subject}:{from_tag}>{to_tag}:{day}"
                payload = {**facts, "event": event, "from_tag": from_tag, "to_tag": to_tag}
                claimed = database.execute(
                    "INSERT INTO transition_events"
                    "(key,market,subject,event,score,status,payload_json,day,created_at) "
                    "VALUES(?,?,?,?,?,'pending',?,?,?) ON CONFLICT(key) DO NOTHING RETURNING key",
                    (
                        key,
                        market,
                        subject,
                        event,
                        interest(event, facts, has_call),
                        json.dumps(payload),
                        day,
                        at.isoformat(),
                    ),
                ).fetchone()
                stored += 1 if claimed else 0
        unchanged = (
            before is not None
            and seen is not None
            and before["tag"] == facts["tag"]
            and (None if before["ratified"] is None else bool(before["ratified"])) == known_ratified
            and at - seen < STATE_REFRESH
        )
        if unchanged:
            continue
        database.execute(
            "INSERT INTO transition_state(market,subject,tag,ratified,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(market,subject) DO UPDATE SET "
            "tag=excluded.tag,ratified=excluded.ratified,updated_at=excluded.updated_at",
            (
                market,
                subject,
                facts["tag"],
                None if known_ratified is None else int(known_ratified),
                at.isoformat(),
            ),
        )
    return stored


def _transition_waiting(database: Any) -> bool:
    return (
        database.execute(
            "SELECT 1 FROM telegram_outbox_items i JOIN telegram_outbox o ON o.id=i.outbox_id "
            "WHERE i.kind='transition' AND o.status IN ('pending','retry','sending') LIMIT 1"
        ).fetchone()
        is not None
    )


def queue_transitions(database: Any, config: Any, *, origin: str, at: datetime) -> int:
    """Detect changes, then queue the best pending one inside its cap; returns 0 or 1."""

    from runner_web.telegram import format_transition_post_md, memecoin_alerts_enabled
    from runner_web.telegram_outbox import enqueue_cards

    markets = ["stock"] + (["memecoin"] if memecoin_alerts_enabled() else [])
    for market in markets:
        try:
            observed = (
                _stock_observations(database, at)
                if market == "stock"
                else _memecoin_observations(database, at)
            )
            detect(database, market, observed, at)
        except Exception:
            LOG.warning("Transition detection failed for %s", market, exc_info=True)
    oldest = (at - timedelta(minutes=max_age_minutes())).isoformat()
    database.execute(
        "UPDATE transition_events SET status='stale' WHERE status='pending' AND created_at<?",
        (oldest,),
    )
    if _transition_waiting(database):
        return 0
    day = at.date().isoformat()
    open_markets = []
    for market in markets:
        used = database.execute(
            "SELECT COUNT(*) AS n FROM transition_events WHERE market=? AND day=? "
            "AND status='queued'",
            (market, day),
        ).fetchone()["n"]
        if used < daily_cap(market):
            open_markets.append(market)
    if not open_markets:
        return 0
    cooldown = (at - timedelta(seconds=_int_env("TELEGRAM_TICKER_QUIET_SECONDS", 1800))).isoformat()
    marks = ",".join("?" for _ in open_markets)
    rows = database.execute(
        f"SELECT key,subject,payload_json,created_at FROM transition_events "
        f"WHERE status='pending' AND market IN ({marks}) ORDER BY score DESC,created_at ASC "
        "LIMIT 20",
        tuple(open_markets),
    ).fetchall()
    for row in rows:
        recent = database.execute(
            "SELECT 1 FROM transition_events WHERE subject=? AND status='queued' "
            "AND created_at>? LIMIT 1",
            (row["subject"], cooldown),
        ).fetchone()
        if recent:
            continue
        payload = json.loads(row["payload_json"])
        text = format_transition_post_md(payload, origin=origin)
        if not text:
            database.execute(
                "UPDATE transition_events SET status='dropped' WHERE key=?", (row["key"],)
            )
            continue
        created = _stamp(row["created_at"]) or at
        card = {
            "kind": "transition",
            "subject": row["key"],
            "ticker": payload["ticker"]
            if payload["market"] == "stock"
            else "coin:" + str(payload.get("coin_id") or payload["subject"]),
            "text": text,
            "expires_at": (created + timedelta(minutes=max_age_minutes())).isoformat()
            if max_age_minutes()
            else None,
        }
        if card["expires_at"] is None:
            del card["expires_at"]
        queued = enqueue_cards(database, config.chat_id, [card], at=at)
        database.execute("UPDATE transition_events SET status='queued' WHERE key=?", (row["key"],))
        return queued
    return 0
