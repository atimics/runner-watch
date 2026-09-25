"""Saved pregame game-winner prices from named prediction-market venues."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from runner_watch.ingestion import SourceFetch
from runner_web.db import connection
from runner_web.ingestion import record_source_fetch

KALSHI_ROOT = "https://external-api.kalshi.com/trade-api/v2"
POLY_ROOT = "https://gamma-api.polymarket.com"
KALSHI_SERIES = {
    "mlb": "KXMLBGAME",
    "nfl": "KXNFLGAME",
    "nba": "KXNBAGAME",
    "nhl": "KXNHLGAME",
}
# Venue symbols vary from the scoreboard. Every alias is league and venue specific.
ALIASES = {
    "kalshi": {"nfl": {"JAX": "JAC", "WSH": "WAS"}, "nhl": {"CGY": "CGY"}},
    "polymarket": {
        "mlb": {"CHW": "cws", "CWS": "cws", "SFG": "sf", "SF": "sf"},
        "nfl": {"JAC": "jax", "JAX": "jax", "WSH": "was"},
        "nhl": {"CGY": "cal", "NJD": "nj", "NJ": "nj", "SJS": "sj", "SJ": "sj"},
    },
}
ET = ZoneInfo("America/New_York")
PAIR = re.compile(r"^([A-Z0-9]+) vs ([A-Z0-9]+) \(")
POLY_SLUG = re.compile(r"^(mlb|nfl|nba|nhl)-([a-z0-9]+)-([a-z0-9]+)-(\d{4}-\d{2}-\d{2})$")


def _time(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else result.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _symbol(event: dict[str, Any], side: str, source: str) -> str:
    team = event.get(side) or {}
    raw = str(team.get("abbreviation") or event.get(f"{side}_abbreviation") or "").upper()
    return ALIASES.get(source, {}).get(str(event["league"]), {}).get(raw, raw)


def _candidates(events: list[dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("status") == "pre"
        and event.get("season_type") not in {"preseason", "pre-season", "spring-training"}
        and (start := _time(event.get("start_time"))) is not None
        and at < start <= at + timedelta(days=45)
    ]


def _game_key(event: dict[str, Any], source: str) -> tuple[str, str, str]:
    return (
        _symbol(event, "away", source).upper(),
        _symbol(event, "home", source).upper(),
        str(event["league"]),
    )


def _price_pair(away: float | None, home: float | None) -> tuple[float, float] | None:
    if away is None or home is None or not (0 <= away <= 1 and 0 <= home <= 1):
        return None
    total = away + home
    if not 0.85 <= total <= 1.15:
        return None
    return away / total, home / total


def _reading(
    *,
    event: dict[str, Any],
    source: str,
    source_event_id: str,
    market_id: str,
    away: float,
    home: float,
    source_updated_at: str,
    source_url: str,
    at: datetime,
    price_basis: str,
) -> dict[str, Any]:
    digest = hashlib.sha256(
        json.dumps(
            [source, market_id, round(away, 6), round(home, 6)], separators=(",", ":")
        ).encode()
    ).hexdigest()
    return {
        "event_id": str(event["id"]),
        "source": source,
        "source_event_id": source_event_id,
        "source_market_id": market_id,
        "away_probability": away,
        "home_probability": home,
        "source_updated_at": source_updated_at,
        "source_url": source_url,
        "price_basis": price_basis,
        "observed_at": at.isoformat(),
        "quote_hash": digest,
    }


def normalize_kalshi(
    events: list[dict[str, Any]],
    raw_events: list[dict[str, Any]],
    at: datetime,
) -> list[dict[str, Any]]:
    available = _candidates(events, at)
    readings = []
    for raw in raw_events:
        series = str(raw.get("series_ticker") or "")
        league = next((key for key, value in KALSHI_SERIES.items() if value == series), None)
        if league is None or (raw.get("product_metadata") or {}).get("competition_scope") != "Game":
            continue
        if "preseason" in str((raw.get("product_metadata") or {}).get("competition", "")).lower():
            continue
        match = PAIR.match(str(raw.get("sub_title") or ""))
        date_match = re.search(r"-(\d{2}[A-Z]{3}\d{2})", str(raw.get("event_ticker") or ""))
        if not match or not date_match:
            continue
        try:
            game_date = datetime.strptime(date_match.group(1), "%y%b%d").date()
        except ValueError:
            continue
        matching = [
            event
            for event in available
            if _game_key(event, "kalshi") == (match.group(1), match.group(2), league)
            and _time(event["start_time"]).astimezone(ET).date() == game_date
        ]
        if len(matching) != 1:
            continue
        event = matching[0]
        markets = {
            str(market.get("ticker") or "").rsplit("-", 1)[-1]: market
            for market in raw.get("markets") or []
            if market.get("status") == "active" and market.get("market_type") == "binary"
        }
        prices = []
        for side in ("away", "home"):
            market = markets.get(_symbol(event, side, "kalshi"))
            if not market or not str(market.get("title") or "").endswith(" wins"):
                break
            bid, ask = (
                _number(market.get("yes_bid_dollars")),
                _number(market.get("yes_ask_dollars")),
            )
            if bid is None or ask is None or not (0 <= bid <= ask <= 1) or ask - bid > 0.2:
                break
            prices.append((market, (bid + ask) / 2))
        if len(prices) != 2 or (pair := _price_pair(prices[0][1], prices[1][1])) is None:
            continue
        updated = max((_time(m.get("updated_time")) for m, _ in prices), default=None)
        if updated is None or updated > at or updated >= _time(event["start_time"]):
            continue
        ticker = str(raw["event_ticker"])
        readings.append(
            _reading(
                event=event,
                source="kalshi",
                source_event_id=ticker,
                market_id=ticker,
                away=pair[0],
                home=pair[1],
                source_updated_at=updated.isoformat(),
                source_url=f"{KALSHI_ROOT}/events/{ticker}",
                at=at,
                price_basis="Yes bid/ask midpoint, normalized across the two game winners",
            )
        )
    return readings


def _array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def normalize_polymarket(
    events: list[dict[str, Any]],
    raw_events: list[dict[str, Any]],
    at: datetime,
) -> list[dict[str, Any]]:
    available = _candidates(events, at)
    readings = []
    for raw in raw_events:
        match = POLY_SLUG.fullmatch(str(raw.get("slug") or ""))
        if not match or raw.get("closed") or not raw.get("active"):
            continue
        league, away_symbol, home_symbol, _date = match.groups()
        start = _time(raw.get("startTime"))
        if start is None:
            continue
        matching = [
            event
            for event in available
            if _game_key(event, "polymarket") == (away_symbol.upper(), home_symbol.upper(), league)
            and abs((_time(event["start_time"]) - start).total_seconds()) <= 7200
        ]
        if len(matching) != 1:
            continue
        event = matching[0]
        market = next(
            (
                market
                for market in raw.get("markets") or []
                if market.get("sportsMarketType") == "moneyline"
                and market.get("active")
                and not market.get("closed")
            ),
            None,
        )
        if market is None:
            continue
        outcomes = _array(market.get("outcomes"))
        prices = _array(market.get("outcomePrices"))
        if len(outcomes) != 2 or len(prices) != 2:
            continue
        away_name = str((event.get("away") or {}).get("name") or event.get("away_team_name") or "")
        home_name = str((event.get("home") or {}).get("name") or event.get("home_team_name") or "")
        if not away_name or not home_name:
            continue
        away_aliases = {away_name.lower(), away_name.split()[-1].lower()}
        home_aliases = {home_name.lower(), home_name.split()[-1].lower()}
        labels = [str(label).strip().lower() for label in outcomes]
        if len(set(labels)) != 2:
            continue
        away_index = next(
            (index for index, label in enumerate(labels) if label in away_aliases), None
        )
        home_index = next(
            (index for index, label in enumerate(labels) if label in home_aliases), None
        )
        if away_index is None or home_index is None or away_index == home_index:
            continue
        pair = _price_pair(_number(prices[away_index]), _number(prices[home_index]))
        updated = _time(market.get("updatedAt"))
        if pair is None or updated is None or updated > at or updated >= _time(event["start_time"]):
            continue
        slug = str(raw["slug"])
        readings.append(
            _reading(
                event=event,
                source="polymarket",
                source_event_id=str(raw.get("id") or slug),
                market_id=str(market.get("id") or slug),
                away=pair[0],
                home=pair[1],
                source_updated_at=updated.isoformat(),
                source_url=f"https://polymarket.com/event/{slug}",
                at=at,
                price_basis="Listed moneyline outcome prices, normalized across both teams",
            )
        )
    return readings


def _get_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; RATi sports preview/1.0)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.load(response)


def fetch_kalshi(league: str, at: datetime) -> list[dict[str, Any]]:
    series = KALSHI_SERIES[league]
    result = []
    cursor = ""
    for _ in range(2):
        query = {
            "series_ticker": series,
            "status": "open",
            "with_nested_markets": "true",
            "limit": 200,
            "min_close_ts": int(at.timestamp()),
        }
        if cursor:
            query["cursor"] = cursor
        payload = _get_json(f"{KALSHI_ROOT}/events?{urlencode(query)}")
        result.extend(payload.get("events") or [])
        cursor = str(payload.get("cursor") or "")
        if not cursor:
            break
    return result


def fetch_polymarket(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slugs = set()
    for event in events:
        start = _time(event["start_time"])
        if start is None:
            continue
        away, home, league = _game_key(event, "polymarket")
        for day in {start.date(), start.astimezone(ET).date()}:
            slugs.add(f"{league}-{away.lower()}-{home.lower()}-{day.isoformat()}")
    result = []
    ordered = sorted(slugs)
    for index in range(0, len(ordered), 30):
        query = urlencode([("slug", slug) for slug in ordered[index : index + 30]])
        result.extend(_get_json(f"{POLY_ROOT}/events?{query}"))
    return result


def store_readings(readings: list[dict[str, Any]]) -> int:
    inserted = 0
    with connection() as database:
        for row in readings:
            cursor = database.execute(
                """INSERT INTO sports_prediction_market_snapshots(
                    id,event_id,source,source_event_id,source_market_id,away_probability,
                    home_probability,source_updated_at,source_url,price_basis,observed_at,quote_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id,source,quote_hash) DO NOTHING""",
                (
                    str(uuid.uuid4()),
                    row["event_id"],
                    row["source"],
                    row["source_event_id"],
                    row["source_market_id"],
                    row["away_probability"],
                    row["home_probability"],
                    row["source_updated_at"],
                    row["source_url"],
                    row["price_basis"],
                    row["observed_at"],
                    row["quote_hash"],
                ),
            )
            inserted += max(0, cursor.rowcount)
    return inserted


def refresh_prediction_markets(
    events: list[dict[str, Any]], at: datetime | None = None
) -> dict[str, Any]:
    current = (at or datetime.now(UTC)).astimezone(UTC)
    available = _candidates(events, current)
    if os.getenv("SPORTS_PREDICTION_MARKETS_ENABLED", "false").lower() in {"0", "false", "off"}:
        return {"enabled": False, "saved": {}, "errors": {}}
    saved: dict[str, int] = {}
    errors: dict[str, str] = {}
    for league in sorted({str(event["league"]) for event in available}):
        started = datetime.now(UTC)
        locator = f"{KALSHI_ROOT}/events?series_ticker={KALSHI_SERIES[league]}"
        try:
            raw = fetch_kalshi(league, current)
            record_source_fetch(
                SourceFetch.success(
                    source="kalshi",
                    feed="sports_game_winner_prices",
                    locator=locator,
                    started_at=started,
                    payload={"league": league, "event_count": len(raw)},
                    metadata={"league": league, "received_count": len(raw)},
                )
            )
            saved[f"kalshi:{league}"] = store_readings(normalize_kalshi(available, raw, current))
        except Exception as exc:
            record_source_fetch(
                SourceFetch.failure(
                    source="kalshi",
                    feed="sports_game_winner_prices",
                    locator=locator,
                    started_at=started,
                    error=exc,
                    metadata={"league": league},
                )
            )
            errors[f"kalshi:{league}"] = str(exc)[:160]
    if available:
        started = datetime.now(UTC)
        locator = f"{POLY_ROOT}/events"
        try:
            raw = fetch_polymarket(available)
            record_source_fetch(
                SourceFetch.success(
                    source="polymarket",
                    feed="sports_game_winner_prices",
                    locator=locator,
                    started_at=started,
                    payload={"event_count": len(raw)},
                    metadata={"received_count": len(raw)},
                )
            )
            saved["polymarket"] = store_readings(normalize_polymarket(available, raw, current))
        except Exception as exc:
            record_source_fetch(
                SourceFetch.failure(
                    source="polymarket",
                    feed="sports_game_winner_prices",
                    locator=locator,
                    started_at=started,
                    error=exc,
                )
            )
            errors["polymarket"] = str(exc)[:160]
    return {"enabled": True, "saved": saved, "errors": errors}


def event_readings(
    event_id: str, start_time: str
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    with connection() as database:
        rows = database.execute(
            """SELECT * FROM sports_prediction_market_snapshots
            WHERE event_id=? AND source_updated_at<? ORDER BY observed_at,id""",
            (event_id, start_time),
        ).fetchall()
    history: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        history.setdefault(str(row["source"]), []).append(dict(row))
    latest = [readings[-1] for readings in history.values()]
    return latest, history
