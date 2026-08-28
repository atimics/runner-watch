from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
import urllib.request
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any

from runner_watch.ingestion import SourceFetch
from runner_web import db as runner_db
from runner_web.ai_kol import (
    FLASH,
    KOL_LADDER_SIZE,
    SPORTS_FORECAST_CONTRACT_VERSION,
    actor_snapshot,
    model_display_name,
)
from runner_web.caller_ids import ensure_caller_identity_with_database
from runner_web.db import connection
from runner_web.flash_wallet import credit_flash, sports_call_reward
from runner_web.ingestion import record_source_fetch
from runner_web.odds_api import (
    BOOKMAKER_FRESHNESS,
    BOOKMAKER_FUTURE_TOLERANCE,
    BOOKMAKER_MAX_AGE,
    CONSENSUS_SPORTSBOOK,
    MIN_CONSENSUS_BOOKS,
    OddsApiConfig,
    OddsApiError,
    Quota,
    apply_moneylines,
    can_spend,
    clear_event_odds,
    fetch_moneylines,
    last_recorded_quota,
    mark_refresh_attempt,
    probe_quota,
    refresh_decision,
)
from runner_web.odds_api import PROVIDER as ODDS_PROVIDER

MODEL_VERSION = "team-form-v1"
SOURCE = "espn"
FEED = "sports_scoreboard_preview"
HISTORY_FEED = "sports_scoreboard_history"
PLAYER_FEED = "sports_boxscore_preview"
NEWS_FEED = "sports_news_preview"
SOURCE_URL = "https://site.api.espn.com/apis/site/v2/sports"
PROMOTED_SIGNALS = {"lean", "watch"}
NEWS_MAX_AGE = timedelta(days=7)
NEWS_PER_EVENT = 6
SERIES_WINDOW = timedelta(days=7)
SERIES_MAX_GAP = timedelta(hours=52)
BACK_TO_BACK_MAX_GAP = timedelta(hours=30)
HISTORY_TARGET_DAYS = 210
HISTORY_CHUNK_DAYS = 5
ALPHA_HISTORY_POINTS = 160
MODEL_SCORECARD_TARGET = 250
LEAGUES = {
    "mlb": {"sport": "baseball", "path": "mlb", "name": "MLB", "home_edge": 0.035},
    "nfl": {"sport": "football", "path": "nfl", "name": "NFL", "home_edge": 0.055},
    "nba": {"sport": "basketball", "path": "nba", "name": "NBA", "home_edge": 0.060},
    "nhl": {"sport": "hockey", "path": "nhl", "name": "NHL", "home_edge": 0.040},
}
SCORE_MODELS = {
    "mlb": {"total": 8.6, "exponent": 1.83, "decimals": 1},
    "nfl": {"total": 44.5, "exponent": 2.37, "decimals": 0},
    "nba": {"total": 228.0, "exponent": 13.91, "decimals": 0},
    "nhl": {"total": 6.1, "exponent": 2.0, "decimals": 1},
}
BOVADA_BOOKMAKER_KEY = "bovada"
MODEL_RECORD_CACHE_TTL_SECONDS = 300.0
_MODEL_RECORD_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_MODEL_RECORD_CACHE_LOCK = threading.Lock()


def _clear_model_record_cache() -> None:
    with _MODEL_RECORD_CACHE_LOCK:
        _MODEL_RECORD_CACHE.clear()


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_time(value: Any) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _american(value: Any) -> int | None:
    try:
        return int(str(value).replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def implied_probability(american_odds: int | None) -> float | None:
    if american_odds is None or american_odds == 0:
        return None
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return -american_odds / (-american_odds + 100)


def no_vig_probabilities(
    home_odds: int | None, away_odds: int | None
) -> tuple[float | None, float | None]:
    home = implied_probability(home_odds)
    away = implied_probability(away_odds)
    if home is None or away is None or home + away <= 0:
        return None, None
    total = home + away
    return home / total, away / total


def _record(summary: Any) -> tuple[int, int] | None:
    parts = str(summary or "").split("-")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _overall_record(competitor: dict[str, Any]) -> tuple[int, int] | None:
    records = competitor.get("records") or []
    overall = next(
        (item for item in records if str(item.get("name", "")).lower() == "overall"),
        records[0] if records else None,
    )
    return _record(overall.get("summary")) if overall else None


def _team(competitor: dict[str, Any]) -> dict[str, Any]:
    team = competitor.get("team") or {}
    record = _overall_record(competitor)
    return {
        "id": str(team.get("id") or ""),
        "name": str(team.get("displayName") or team.get("name") or "Unknown"),
        "abbreviation": str(team.get("abbreviation") or "—"),
        "score": _number(competitor.get("score")),
        "record": f"{record[0]}-{record[1]}" if record else None,
        "wins": record[0] if record else None,
        "losses": record[1] if record else None,
    }


def _close_value(side: dict[str, Any], field: str) -> Any:
    close = side.get("close") or {}
    return close.get(field)


def normalize_event(league: str, event: dict[str, Any]) -> dict[str, Any] | None:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0]
    competitors = competition.get("competitors") or []
    home_raw = next((item for item in competitors if item.get("homeAway") == "home"), None)
    away_raw = next((item for item in competitors if item.get("homeAway") == "away"), None)
    if not home_raw or not away_raw:
        return None
    home = _team(home_raw)
    away = _team(away_raw)
    odds_list = competition.get("odds") or []
    odds = odds_list[0] if odds_list else {}
    moneyline = odds.get("moneyline") or {}
    home_market = moneyline.get("home") or {}
    away_market = moneyline.get("away") or {}
    home_odds = _american(_close_value(home_market, "odds"))
    away_odds = _american(_close_value(away_market, "odds"))
    status = (event.get("status") or {}).get("type") or {}
    season = event.get("season") or {}
    venue = competition.get("venue") or {}
    address = venue.get("address") or {}
    event_id = f"{league}:{event.get('id')}"
    return {
        "id": event_id,
        "external_id": str(event.get("id") or ""),
        "league": league,
        "league_name": LEAGUES[league]["name"],
        "season_type": str(season.get("slug") or "unknown"),
        "name": str(event.get("name") or f"{away['name']} at {home['name']}"),
        "start_time": _parse_time(event.get("date")),
        "status": str(status.get("state") or "pre"),
        "status_detail": str(status.get("shortDetail") or status.get("detail") or "Scheduled"),
        "completed": bool(status.get("completed")),
        "home": home,
        "away": away,
        "venue": str(venue.get("fullName") or ""),
        "location": ", ".join(
            part
            for part in (
                str(address.get("city") or ""),
                str(address.get("state") or ""),
            )
            if part
        ),
        "sportsbook": str((odds.get("provider") or {}).get("name") or ""),
        "home_odds": home_odds,
        "away_odds": away_odds,
        "home_open_odds": _american((home_market.get("open") or {}).get("odds")),
        "away_open_odds": _american((away_market.get("open") or {}).get("odds")),
        "spread": _number(odds.get("spread")),
        "total": _number(odds.get("overUnder")),
        "source_url": (
            f"https://www.espn.com/{LEAGUES[league]['sport']}/game/_/gameId/{event.get('id')}"
        ),
    }


def predict_event(event: dict[str, Any]) -> dict[str, Any]:
    home = event["home"]
    away = event["away"]
    home_record = (
        (int(home["wins"]), int(home["losses"]))
        if home.get("wins") is not None and home.get("losses") is not None
        else None
    )
    away_record = (
        (int(away["wins"]), int(away["losses"]))
        if away.get("wins") is not None and away.get("losses") is not None
        else None
    )
    evidence: list[str] = []
    risks = ["Baseline model does not include lineups, injuries, or starting players."]
    eligible_season = event.get("season_type") in {
        "regular-season",
        "post-season",
        "postseason",
    }
    if event.get("season_type") == "unknown":
        risks.append(
            "The competition type is not verified, so this game is not eligible for a signal."
        )
    if not eligible_season:
        risks.append("Exhibition and preseason records are not used to promote an edge.")
    if home_record and away_record:
        home_rate = (home_record[0] + 8) / (sum(home_record) + 16)
        away_rate = (away_record[0] + 8) / (sum(away_record) + 16)
        probability = 0.5 + (home_rate - away_rate) * 0.65
        evidence.append(
            f"Overall season records: {away['name']} {away['record']}, "
            f"{home['name']} {home['record']}."
        )
        quality = "baseline"
    else:
        probability = 0.5
        risks.append("One or both season records are missing.")
        quality = "thin"
    probability += float(LEAGUES[event["league"]]["home_edge"])
    home_probability = max(0.18, min(0.82, probability))
    away_probability = 1 - home_probability
    home_market, away_market = no_vig_probabilities(event["home_odds"], event["away_odds"])
    market_book_count = int(event.get("market_book_count") or 0)
    market_is_consensus = bool(event.get("market_is_consensus"))
    if home_market is not None and away_market is not None:
        if market_is_consensus:
            evidence.append(f"Fresh market consensus: {market_book_count} sportsbooks.")
        evidence.append(
            f"No-vig market: {home['abbreviation']} {home_market:.1%}, "
            f"{away['abbreviation']} {away_market:.1%}."
        )
        home_edge = home_probability - home_market
        away_edge = away_probability - away_market
        if not eligible_season:
            selection = "pass"
            edge = None
            signal = "pass"
            quality = "thin"
        elif max(home_edge, away_edge) >= 0.02:
            selection = "home" if home_edge >= away_edge else "away"
            edge = max(home_edge, away_edge)
            signal = "lean" if edge < 0.05 else "watch"
        else:
            selection = "pass"
            edge = max(home_edge, away_edge)
            signal = "pass"
    else:
        selection = "home" if home_probability >= away_probability else "away"
        edge = None
        signal = "model only"
        risks.append("A complete two-sided moneyline is not available.")
    if event.get("odds_provider") == ODDS_PROVIDER and not market_is_consensus:
        risks.append(
            f"Only {market_book_count} fresh sportsbook line"
            f"{'s are' if market_book_count != 1 else ' is'} available; "
            f"{MIN_CONSENSUS_BOOKS} are required for a market signal."
        )
        selection = "pass"
        edge = None
        signal = "pass"
        quality = "thin"
    selected_probability = (
        home_probability
        if selection == "home"
        else away_probability
        if selection == "away"
        else None
    )
    selected_team = (
        home["name"] if selection == "home" else away["name"] if selection == "away" else "No pick"
    )
    return {
        "model_version": MODEL_VERSION,
        "home_probability": round(home_probability, 6),
        "away_probability": round(away_probability, 6),
        "home_market_probability": round(home_market, 6) if home_market is not None else None,
        "away_market_probability": round(away_market, 6) if away_market is not None else None,
        "selection": selection,
        "selected_team": selected_team,
        "selected_probability": round(selected_probability, 6) if selected_probability else None,
        "edge": round(edge, 6) if edge is not None else None,
        "edge_pct": round(edge * 100, 1) if edge is not None else None,
        "signal": signal,
        "quality": quality,
        "evidence": evidence,
        "risks": risks,
    }


def _probability(value: Any) -> float | None:
    probability = _number(value)
    if probability is None:
        return None
    if 1 < probability <= 100:
        probability /= 100
    return probability if 0 <= probability <= 1 else None


def validate_sports_ai_forecast(
    value: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Normalize one independent, pregame AI winner forecast."""

    if not isinstance(value, dict):
        raise ValueError("sports forecast is missing")
    raw_home_probability = _number(value.get("home_probability"))
    raw_away_probability = _number(value.get("away_probability"))
    if (
        raw_home_probability is not None
        and raw_away_probability is not None
        and max(raw_home_probability, raw_away_probability) > 1
    ):
        home_probability = _probability(raw_home_probability / 100)
        away_probability = _probability(raw_away_probability / 100)
    else:
        home_probability = _probability(raw_home_probability)
        away_probability = _probability(raw_away_probability)
    if home_probability is None and away_probability is not None:
        home_probability = 1 - away_probability
    if away_probability is None and home_probability is not None:
        away_probability = 1 - home_probability
    if home_probability is None or away_probability is None:
        raise ValueError("sports forecast probabilities are missing")
    total = home_probability + away_probability
    if not 0.98 <= total <= 1.02:
        raise ValueError("sports forecast probabilities must sum to 1")
    home_probability = home_probability / total
    away_probability = 1 - home_probability

    teams = evidence.get("teams") or {}
    raw_selection = str(value.get("selection") or "").strip().casefold()
    selection_aliases = {
        "home": "home",
        "away": "away",
        "pass": "pass",
        "no_call": "pass",
    }
    for side in ("home", "away"):
        team = teams.get(side) or {}
        for candidate in (team.get("id"), team.get("name"), team.get("abbreviation")):
            if candidate:
                selection_aliases[str(candidate).strip().casefold()] = side
    selection = selection_aliases.get(raw_selection)
    if selection not in {"home", "away", "pass"}:
        raise ValueError("sports forecast selection is invalid")
    if selection == "home" and home_probability < away_probability:
        raise ValueError("sports forecast selection conflicts with its probabilities")
    if selection == "away" and away_probability < home_probability:
        raise ValueError("sports forecast selection conflicts with its probabilities")

    maximum = max(home_probability, away_probability)
    default_confidence = "high" if maximum >= 0.65 else "medium" if maximum >= 0.56 else "low"
    confidence = str(value.get("confidence") or default_confidence).strip().lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = default_confidence
    reason = " ".join(str(value.get("reason") or "").split())[:500]
    if not reason:
        raise ValueError("sports forecast reason is missing")
    return {
        "contract_version": SPORTS_FORECAST_CONTRACT_VERSION,
        "selection": selection,
        "home_probability": round(home_probability, 6),
        "away_probability": round(away_probability, 6),
        "confidence": confidence,
        "reason": reason,
    }


def _scoreboard_url(league: str, start: date, end: date) -> str:
    config = LEAGUES[league]
    date_range = f"{start:%Y%m%d}-{end:%Y%m%d}"
    return (
        f"{SOURCE_URL}/{config['sport']}/{config['path']}/scoreboard?dates={date_range}&limit=100"
    )


def _fetch_league_range(
    league: str,
    start: date,
    end: date,
    *,
    feed: str,
) -> list[dict[str, Any]]:
    if league not in LEAGUES:
        raise ValueError("Unsupported league")
    locator = _scoreboard_url(league, start, end)
    started = datetime.now(UTC)
    request = urllib.request.Request(locator)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            body = response.read()
        payload = json.loads(body)
        events = [
            normalized
            for raw in payload.get("events", [])
            if (normalized := normalize_event(league, raw)) is not None
        ]
        record_source_fetch(
            SourceFetch.success(
                source=SOURCE,
                feed=feed,
                locator=locator,
                started_at=started,
                payload={
                    "league": league,
                    "event_count": len(events),
                    "event_ids": [event["external_id"] for event in events],
                },
                content_type="application/json",
                metadata={"league": league, "received_count": len(events)},
            )
        )
        return events
    except Exception as exc:
        record_source_fetch(
            SourceFetch.failure(
                source=SOURCE,
                feed=feed,
                locator=locator,
                started_at=started,
                error=exc,
                metadata={"league": league},
            )
        )
        raise


def fetch_league(league: str, at: datetime | None = None) -> list[dict[str, Any]]:
    current = at or datetime.now(UTC)
    return _fetch_league_range(
        league,
        current.date() - timedelta(days=1),
        current.date() + timedelta(days=3),
        feed=FEED,
    )


def _history_cursor(league: str, current: datetime) -> date | None:
    key = f"sports_history_cursor:{league}"
    with connection() as database:
        row = database.execute(
            "SELECT value FROM worker_state WHERE key=?",
            (key,),
        ).fetchone()
    if row:
        try:
            cursor = date.fromisoformat(str(row["value"]))
        except ValueError:
            cursor = current.date() - timedelta(days=2)
    else:
        cursor = current.date() - timedelta(days=2)
    if cursor < current.date() - timedelta(days=HISTORY_TARGET_DAYS):
        return None
    return cursor


def _advance_history_cursor(league: str, cursor: date, current: datetime) -> None:
    next_cursor = cursor - timedelta(days=HISTORY_CHUNK_DAYS)
    timestamp = _iso(current)
    with connection() as database:
        database.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (f"sports_history_cursor:{league}", next_cursor.isoformat(), timestamp),
        )


def fetch_league_history_chunk(
    league: str,
    at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Fetch one older scoreboard chunk so Alpha fills without a request storm."""

    current = (at or datetime.now(UTC)).astimezone(UTC)
    cursor = _history_cursor(league, current)
    if cursor is None:
        return []
    start = cursor - timedelta(days=HISTORY_CHUNK_DAYS - 1)
    events = _fetch_league_range(league, start, cursor, feed=HISTORY_FEED)
    _advance_history_cursor(league, cursor, current)
    return events


def _league_news_url(league: str) -> str:
    config = LEAGUES[league]
    return f"{SOURCE_URL}/{config['sport']}/{config['path']}/news?limit=50"


def _optional_time(value: Any) -> datetime | None:
    try:
        return _parse_time(value)
    except (TypeError, ValueError):
        return None


def _article_team_ids(article: dict[str, Any]) -> set[str]:
    team_ids: set[str] = set()
    for category in article.get("categories") or []:
        if not isinstance(category, dict) or category.get("type") != "team":
            continue
        team = category.get("team") or {}
        team_id = str(team.get("id") or category.get("teamId") or "").strip()
        if team_id:
            team_ids.add(team_id)
    return team_ids


def _article_event_ids(article: dict[str, Any]) -> set[str]:
    event_ids: set[str] = set()
    for category in article.get("categories") or []:
        if not isinstance(category, dict) or category.get("type") != "event":
            continue
        event = category.get("event") or {}
        event_id = str(event.get("id") or category.get("eventId") or "").strip()
        if event_id:
            event_ids.add(event_id)
    return event_ids


def normalize_news_articles(
    events: list[dict[str, Any]],
    payload: dict[str, Any],
    collected_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Match recent headlines to exact games first, then fall back to source team IDs."""

    current = collected_at or datetime.now(UTC)
    promoted = [event for event in events if predict_event(event).get("signal") in PROMOTED_SIGNALS]
    if not promoted:
        return []
    promoted_ids = {str(event["id"]) for event in promoted}
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return []
    matched: list[dict[str, Any]] = []
    event_counts: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for raw in articles:
        if not isinstance(raw, dict):
            continue
        headline = " ".join(str(raw.get("headline") or "").split())
        links = raw.get("links") or {}
        source_url = str(((links.get("web") or {}).get("href")) or "").strip()
        if not headline or not source_url.startswith(("https://", "http://")):
            continue
        published = _optional_time(raw.get("published") or raw.get("lastModified")) or current
        if published < current - NEWS_MAX_AGE or published > current + timedelta(days=1):
            continue
        source_team_ids = _article_team_ids(raw)
        source_event_ids = _article_event_ids(raw)
        if not source_event_ids and not source_team_ids:
            continue
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            external_id = hashlib.sha256(source_url.encode()).hexdigest()[:32]
        summary = " ".join(str(raw.get("description") or "").split())[:500]
        source_name = str(raw.get("source") or "ESPN").strip()[:120] or "ESPN"
        for event in events:
            event_id = str(event["id"])
            if not source_event_ids and event_id not in promoted_ids:
                continue
            if event_counts.get(event_id, 0) >= NEWS_PER_EVENT:
                continue
            if source_event_ids and str(event.get("external_id") or "") not in source_event_ids:
                continue
            home_match = str(event["home"]["id"]) in source_team_ids
            away_match = str(event["away"]["id"]) in source_team_ids
            if not source_event_ids and not home_match and not away_match:
                continue
            unique_key = (event_id, external_id)
            if unique_key in seen:
                continue
            seen.add(unique_key)
            event_counts[event_id] = event_counts.get(event_id, 0) + 1
            if home_match == away_match:
                team_side = "both"
            elif home_match:
                team_side = "home"
            else:
                team_side = "away"
            article_id = hashlib.sha256(f"{SOURCE}|{event_id}|{external_id}".encode()).hexdigest()[
                :32
            ]
            matched.append(
                {
                    "id": article_id,
                    "event_id": event_id,
                    "provider": SOURCE,
                    "external_id": external_id,
                    "team_side": team_side,
                    "source_name": source_name,
                    "headline": headline[:300],
                    "summary": summary,
                    "source_url": source_url,
                    "published_at": _iso(published),
                    "collected_at": _iso(current),
                }
            )
    return matched


def fetch_league_news(
    league: str,
    events: list[dict[str, Any]],
    at: datetime | None = None,
) -> list[dict[str, Any]]:
    if league not in LEAGUES:
        raise ValueError("Unsupported league")
    locator = _league_news_url(league)
    started = datetime.now(UTC)
    request = urllib.request.Request(locator, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            body = response.read()
        payload = json.loads(body)
        articles = normalize_news_articles(events, payload, at or datetime.now(UTC))
        record_source_fetch(
            SourceFetch.success(
                source=SOURCE,
                feed=NEWS_FEED,
                locator=locator,
                started_at=started,
                payload={
                    "league": league,
                    "articles_received": len(payload.get("articles") or []),
                    "matches": len(articles),
                    "event_ids": sorted({article["event_id"] for article in articles}),
                },
                content_type="application/json",
                metadata={"league": league, "received_count": len(articles)},
            )
        )
        return articles
    except Exception as exc:
        record_source_fetch(
            SourceFetch.failure(
                source=SOURCE,
                feed=NEWS_FEED,
                locator=locator,
                started_at=started,
                error=exc,
                metadata={"league": league},
            )
        )
        raise


def store_news_articles(
    articles: list[dict[str, Any]],
    *,
    replace_event_ids: list[str] | None = None,
) -> int:
    with connection() as database:
        if replace_event_ids:
            event_ids = sorted(set(replace_event_ids))
            placeholders = ",".join("?" for _ in event_ids)
            database.execute(
                f"DELETE FROM sports_news_articles "
                f"WHERE provider=? AND event_id IN ({placeholders})",  # noqa: S608
                (SOURCE, *event_ids),
            )
        for article in articles:
            database.execute(
                """
                INSERT INTO sports_news_articles(
                    id,event_id,provider,external_id,team_side,source_name,headline,
                    summary,source_url,published_at,collected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id,provider,external_id) DO UPDATE SET
                    team_side=excluded.team_side,source_name=excluded.source_name,
                    headline=excluded.headline,summary=excluded.summary,
                    source_url=excluded.source_url,published_at=excluded.published_at,
                    collected_at=excluded.collected_at
                """,
                (
                    article["id"],
                    article["event_id"],
                    article["provider"],
                    article["external_id"],
                    article["team_side"],
                    article["source_name"],
                    article["headline"],
                    article["summary"],
                    article["source_url"],
                    article["published_at"],
                    article["collected_at"],
                ),
            )
    return len(articles)


def _summary_url(event: dict[str, Any]) -> str:
    league = str(event["league"])
    config = LEAGUES[league]
    return f"{SOURCE_URL}/{config['sport']}/{config['path']}/summary?event={event['external_id']}"


def normalize_player_appearances(
    event: dict[str, Any], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Normalize one completed box score into one row per player appearance."""

    home_score = _number(event["home"].get("score"))
    away_score = _number(event["away"].get("score"))
    if home_score is None or away_score is None or home_score == away_score:
        return []
    winning_team_id = str(event["home"]["id"] if home_score > away_score else event["away"]["id"])
    team_lookup = {str(event[side]["id"]): event[side] for side in ("home", "away")}
    appearances: dict[str, dict[str, Any]] = {}
    for team_block in (payload.get("boxscore") or {}).get("players") or []:
        source_team = team_block.get("team") or {}
        team_id = str(source_team.get("id") or "")
        known_team = team_lookup.get(team_id, {})
        team_name = str(
            known_team.get("name")
            or source_team.get("displayName")
            or source_team.get("name")
            or "Unknown"
        )
        team_abbreviation = str(
            known_team.get("abbreviation") or source_team.get("abbreviation") or "—"
        )
        for group_index, statistic_group in enumerate(team_block.get("statistics") or []):
            labels = [str(label) for label in statistic_group.get("labels") or []]
            for player in statistic_group.get("athletes") or []:
                athlete = player.get("athlete") or {}
                player_id = str(athlete.get("id") or "")
                player_name = str(athlete.get("displayName") or "").strip()
                if not player_id or not player_name:
                    continue
                position = player.get("position") or athlete.get("position") or {}
                row = appearances.setdefault(
                    player_id,
                    {
                        "event_id": str(event["id"]),
                        "league": str(event["league"]),
                        "team_id": team_id,
                        "team_name": team_name,
                        "team_abbreviation": team_abbreviation,
                        "player_id": player_id,
                        "player_name": player_name,
                        "position": str(
                            position.get("abbreviation") or position.get("displayName") or ""
                        ),
                        "starter": bool(player.get("starter")),
                        "won": team_id == winning_team_id,
                        "stats": {},
                    },
                )
                values = player.get("stats") or []
                group_name = str(statistic_group.get("name") or f"group_{group_index + 1}")
                row["stats"][group_name] = {
                    labels[index]: value
                    for index, value in enumerate(values)
                    if index < len(labels)
                }
                row["starter"] = bool(row["starter"] or player.get("starter"))
    return list(appearances.values())


def fetch_player_appearances(event: dict[str, Any]) -> list[dict[str, Any]]:
    locator = _summary_url(event)
    started = datetime.now(UTC)
    request = urllib.request.Request(locator)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            body = response.read()
        payload = json.loads(body)
        appearances = normalize_player_appearances(event, payload)
        record_source_fetch(
            SourceFetch.success(
                source=SOURCE,
                feed=PLAYER_FEED,
                locator=locator,
                started_at=started,
                payload={
                    "event_id": event["id"],
                    "player_count": len(appearances),
                    "player_ids": [row["player_id"] for row in appearances],
                },
                content_type="application/json",
                metadata={"league": event["league"], "received_count": len(appearances)},
            )
        )
        return appearances
    except Exception as exc:
        record_source_fetch(
            SourceFetch.failure(
                source=SOURCE,
                feed=PLAYER_FEED,
                locator=locator,
                started_at=started,
                error=exc,
                metadata={"league": event["league"], "event_id": event["id"]},
            )
        )
        raise


def store_player_appearances(
    appearances: list[dict[str, Any]], observed_at: datetime | None = None
) -> int:
    timestamp = _iso(observed_at)
    with connection() as database:
        for row in appearances:
            database.execute(
                """
                INSERT INTO sports_player_appearances(
                    id,event_id,league,team_id,team_name,team_abbreviation,
                    player_id,player_name,position,starter,won,stats_json,collected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id,player_id) DO UPDATE SET
                    team_id=excluded.team_id,team_name=excluded.team_name,
                    team_abbreviation=excluded.team_abbreviation,
                    player_name=excluded.player_name,position=excluded.position,
                    starter=excluded.starter,won=excluded.won,
                    stats_json=excluded.stats_json,collected_at=excluded.collected_at
                """,
                (
                    str(uuid.uuid4()),
                    row["event_id"],
                    row["league"],
                    row["team_id"],
                    row["team_name"],
                    row["team_abbreviation"],
                    row["player_id"],
                    row["player_name"],
                    row["position"],
                    int(row["starter"]),
                    int(row["won"]),
                    _json(row["stats"]),
                    timestamp,
                ),
            )
    return len(appearances)


def _mark_player_boxscore_checked(event_id: str) -> None:
    timestamp = _iso()
    with connection() as database:
        database.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (f"sports_player_checked:{event_id}", "complete", timestamp),
        )


def collect_player_appearances(
    events: list[dict[str, Any]], max_events: int = 12
) -> dict[str, int]:
    """Backfill completed events once; later refreshes reuse the stored box score."""

    candidates = [event for event in events if event.get("completed")]
    if not candidates:
        return {"events": 0, "players": 0, "errors": 0}
    with connection() as database:
        existing = {
            str(row["event_id"])
            for row in database.execute(
                """
                SELECT DISTINCT event_id FROM sports_player_appearances
                WHERE event_id IN ({})
                """.format(",".join("?" for _ in candidates)),  # noqa: S608
                tuple(str(event["id"]) for event in candidates),
            ).fetchall()
        }
    fetched = players = errors = 0
    for event in candidates:
        if str(event["id"]) in existing or fetched >= max(1, max_events):
            continue
        fetched += 1
        try:
            appearances = fetch_player_appearances(event)
            players += store_player_appearances(appearances)
            _mark_player_boxscore_checked(str(event["id"]))
        except Exception:
            errors += 1
    return {"events": fetched, "players": players, "errors": errors}


def collect_stored_player_appearances(
    league: str,
    max_events: int = 3,
) -> dict[str, int]:
    """Fill the newest missing completed box scores from stored Alpha history."""

    with connection() as database:
        rows = database.execute(
            """
            SELECT e.* FROM sports_events e
            WHERE e.league=? AND e.completed=1
              AND e.season_type NOT IN ('preseason','pre-season')
              AND NOT EXISTS(
                SELECT 1 FROM sports_player_appearances a WHERE a.event_id=e.id
              )
              AND NOT EXISTS(
                SELECT 1 FROM worker_state w
                WHERE w.key=('sports_player_checked:' || e.id)
              )
            ORDER BY e.start_time DESC,e.id DESC LIMIT ?
            """,
            (league, max(1, max_events)),
        ).fetchall()
    events = []
    for raw in rows:
        row = dict(raw)
        events.append(
            {
                "id": str(row["id"]),
                "external_id": str(row["external_id"]),
                "league": str(row["league"]),
                "completed": True,
                "home": {
                    "id": str(row["home_team_id"]),
                    "name": str(row["home_team_name"]),
                    "abbreviation": str(row["home_abbreviation"]),
                    "score": row["home_score"],
                },
                "away": {
                    "id": str(row["away_team_id"]),
                    "name": str(row["away_team_name"]),
                    "abbreviation": str(row["away_abbreviation"]),
                    "score": row["away_score"],
                },
            }
        )
    return collect_player_appearances(events, max_events=max_events)


def _input_hash(event: dict[str, Any], prediction: dict[str, Any]) -> str:
    fields = {
        "home_record": event["home"].get("record"),
        "away_record": event["away"].get("record"),
        "home_odds": event.get("home_odds"),
        "away_odds": event.get("away_odds"),
        "season_type": event.get("season_type"),
        "model": prediction["model_version"],
    }
    return hashlib.sha256(_json(fields).encode()).hexdigest()


def _apply_cached_moneylines(events: list[dict[str, Any]]) -> int:
    """Use a recent paid snapshot between scheduled API refreshes."""

    applied = 0
    with connection() as database:
        for event in events:
            if event.get("odds_provider") == ODDS_PROVIDER:
                continue
            row = database.execute(
                """
                SELECT * FROM sports_odds_snapshots
                WHERE event_id=? AND provider=?
                ORDER BY observed_at DESC,id DESC LIMIT 1
                """,
                (event["id"], ODDS_PROVIDER),
            ).fetchone()
            if not row:
                continue
            completed = bool(event.get("completed")) or str(event.get("status")) == "post"
            reference_time = (
                _parse_time(event["start_time"]) if completed else datetime.now(UTC)
            )
            observed_at = _parse_time(row["observed_at"])
            observed_age = reference_time - observed_at
            if not (-BOOKMAKER_FUTURE_TOLERANCE <= observed_age <= BOOKMAKER_MAX_AGE):
                continue
            bookmaker_rows = database.execute(
                """
                SELECT * FROM (
                    SELECT b.*,ROW_NUMBER() OVER(
                        PARTITION BY sportsbook_key ORDER BY observed_at DESC,id DESC
                    ) AS latest_rank
                    FROM sports_bookmaker_odds b
                    WHERE event_id=? AND provider=?
                ) latest WHERE latest_rank=1
                """,
                (event["id"], ODDS_PROVIDER),
            ).fetchall()
            fresh_books = _fresh_bookmaker_items(bookmaker_rows, reference_time)
            book_count = len(fresh_books)
            is_consensus = row["sportsbook"] == CONSENSUS_SPORTSBOOK
            if is_consensus and book_count < MIN_CONSENSUS_BOOKS:
                continue
            if not is_consensus and bookmaker_rows and not fresh_books:
                continue
            event.update(
                {
                    "odds_provider": ODDS_PROVIDER,
                    "sportsbook": row["sportsbook"],
                    "home_odds": row["home_odds"],
                    "away_odds": row["away_odds"],
                    "home_open_odds": row["home_open_odds"],
                    "away_open_odds": row["away_open_odds"],
                    "spread": row["spread"],
                    "total": row["total"],
                    "market_book_count": book_count,
                    "market_is_consensus": is_consensus,
                    "bookmaker_moneylines": [],
                }
            )
            applied += 1
    return applied


def _store_bookmaker_moneylines(
    database: Any,
    event: dict[str, Any],
    timestamp: str,
) -> None:
    for line in event.get("bookmaker_moneylines") or []:
        sportsbook_key = str(line.get("sportsbook_key") or "").strip().lower()
        sportsbook = str(line.get("sportsbook") or "").strip()
        if not sportsbook_key or not sportsbook:
            continue
        quote_hash = hashlib.sha256(
            _json(
                {
                    "sportsbook_key": sportsbook_key,
                    "home_odds": line.get("home_odds"),
                    "away_odds": line.get("away_odds"),
                    "source_updated_at": line.get("last_update"),
                }
            ).encode()
        ).hexdigest()
        database.execute(
            """
            INSERT INTO sports_bookmaker_odds(
                id,event_id,provider,sportsbook_key,sportsbook,market,
                home_odds,away_odds,home_probability,away_probability,
                source_updated_at,quote_hash,observed_at
            ) VALUES(?,?,?,?,?,'moneyline',?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
            """,
            (
                str(uuid.uuid4()),
                event["id"],
                ODDS_PROVIDER,
                sportsbook_key,
                sportsbook,
                int(line["home_odds"]),
                int(line["away_odds"]),
                float(line["home_probability"]),
                float(line["away_probability"]),
                str(line.get("last_update") or "") or None,
                quote_hash,
                timestamp,
            ),
        )


def store_events(events: list[dict[str, Any]], observed_at: datetime | None = None) -> int:
    timestamp = _iso(observed_at)
    with connection() as database:
        for event in events:
            database.execute(
                """
                INSERT INTO sports_events(
                    id,provider,external_id,league,season_type,name,start_time,status,status_detail,
                    completed,home_team_id,home_team_name,home_abbreviation,home_record,
                    home_score,away_team_id,away_team_name,away_abbreviation,away_record,
                    away_score,venue,location,source_url,first_collected_at,last_collected_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    season_type=excluded.season_type,name=excluded.name,
                    start_time=excluded.start_time,status=excluded.status,
                    status_detail=excluded.status_detail,completed=excluded.completed,
                    home_record=excluded.home_record,home_score=excluded.home_score,
                    away_record=excluded.away_record,away_score=excluded.away_score,
                    venue=excluded.venue,location=excluded.location,
                    last_collected_at=excluded.last_collected_at
                """,
                (
                    event["id"],
                    SOURCE,
                    event["external_id"],
                    event["league"],
                    event["season_type"],
                    event["name"],
                    _iso(event["start_time"]),
                    event["status"],
                    event["status_detail"],
                    int(event["completed"]),
                    event["home"]["id"],
                    event["home"]["name"],
                    event["home"]["abbreviation"],
                    event["home"]["record"],
                    event["home"]["score"],
                    event["away"]["id"],
                    event["away"]["name"],
                    event["away"]["abbreviation"],
                    event["away"]["record"],
                    event["away"]["score"],
                    event["venue"],
                    event["location"],
                    event["source_url"],
                    timestamp,
                    timestamp,
                ),
            )
            if event.get("home_odds") is not None or event.get("away_odds") is not None:
                odds_provider = str(event.get("odds_provider") or SOURCE)
                home_open_odds = event.get("home_open_odds")
                away_open_odds = event.get("away_open_odds")
                if home_open_odds is None or away_open_odds is None:
                    opening = database.execute(
                        """
                        SELECT home_odds,away_odds,home_open_odds,away_open_odds
                        FROM sports_odds_snapshots
                        WHERE event_id=? AND provider=?
                        ORDER BY observed_at,id LIMIT 1
                        """,
                        (event["id"], odds_provider),
                    ).fetchone()
                    if opening:
                        home_open_odds = opening["home_open_odds"] or opening["home_odds"]
                        away_open_odds = opening["away_open_odds"] or opening["away_odds"]
                    else:
                        home_open_odds = event.get("home_odds")
                        away_open_odds = event.get("away_odds")
                odds_hash = hashlib.sha256(
                    _json(
                        {
                            "sportsbook": event["sportsbook"],
                            "home": event["home_odds"],
                            "away": event["away_odds"],
                            "home_open": home_open_odds,
                            "away_open": away_open_odds,
                            "spread": event["spread"],
                            "total": event["total"],
                        }
                    ).encode()
                ).hexdigest()
                database.execute(
                    """
                    INSERT INTO sports_odds_snapshots(
                        id,event_id,provider,sportsbook,market,home_odds,away_odds,
                        home_open_odds,away_open_odds,spread,total,snapshot_hash,observed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()),
                        event["id"],
                        odds_provider,
                        event["sportsbook"],
                        "moneyline",
                        event["home_odds"],
                        event["away_odds"],
                        home_open_odds,
                        away_open_odds,
                        event["spread"],
                        event["total"],
                        odds_hash,
                        timestamp,
                    ),
                )
            _store_bookmaker_moneylines(database, event, timestamp)
            prediction = predict_event(event)
            input_hash = _input_hash(event, prediction)
            database.execute(
                """
                INSERT INTO sports_predictions(
                    id,event_id,model_version,input_hash,selection,home_probability,
                    away_probability,home_market_probability,away_market_probability,
                    edge,signal,quality,evidence_json,risks_json,observed_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
                """,
                (
                    str(uuid.uuid4()),
                    event["id"],
                    prediction["model_version"],
                    input_hash,
                    prediction["selection"],
                    prediction["home_probability"],
                    prediction["away_probability"],
                    prediction["home_market_probability"],
                    prediction["away_market_probability"],
                    prediction["edge"],
                    prediction["signal"],
                    prediction["quality"],
                    _json(prediction["evidence"]),
                    _json(prediction["risks"]),
                    timestamp,
                ),
            )
    if events:
        _clear_model_record_cache()
    return len(events)


def settle_picks() -> int:
    current = datetime.now(UTC)
    timestamp = _iso(current)
    settled = 0
    with connection() as database:
        rows = database.execute(
            """
            SELECT p.*,e.home_score,e.away_score,e.completed
            FROM sports_picks p JOIN sports_events e ON e.id=p.event_id
            WHERE p.status='open' AND e.completed=1
            """
        ).fetchall()
        for row in rows:
            home_score = _number(row["home_score"])
            away_score = _number(row["away_score"])
            if home_score is None or away_score is None:
                continue
            if home_score == away_score:
                result, units = "push", 0.0
            else:
                winner = "home" if home_score > away_score else "away"
                if str(row["selection"]) == winner:
                    odds = int(row["american_odds"])
                    result = "win"
                    units = odds / 100 if odds > 0 else 100 / abs(odds)
                else:
                    result, units = "loss", -1.0
            changed = database.execute(
                """
                UPDATE sports_picks SET status='settled',result=?,return_units=?,
                    settled_at=?,updated_at=? WHERE id=? AND status='open'
                """,
                (result, round(units, 4), timestamp, timestamp, row["id"]),
            )
            if changed.rowcount == 1 and result == "win":
                reward = sports_call_reward(row["american_odds"])
                if reward:
                    credit_flash(
                        database,
                        str(row["user_id"]),
                        reward,
                        kind="sports_call_win",
                        reference_id=str(row["id"]),
                        at=current,
                    )
            settled += max(changed.rowcount, 0)
    return settled


def settle_sports_ai_forecasts() -> int:
    """Score frozen AI probabilities after the matching game becomes final."""

    timestamp = _iso()
    settled = 0
    with connection() as database:
        rows = database.execute(
            """
            SELECT f.*,e.home_score,e.away_score
            FROM sports_ai_forecasts f
            JOIN sports_events e ON e.id=f.event_id
            WHERE f.status='open' AND e.completed=1
            """
        ).fetchall()
        for row in rows:
            home_score = _number(row["home_score"])
            away_score = _number(row["away_score"])
            if home_score is None or away_score is None:
                continue
            if home_score == away_score:
                status, result, brier = "void", "void", None
            else:
                home_won = home_score > away_score
                brier = round((float(row["home_probability"]) - int(home_won)) ** 2, 6)
                selection = str(row["selection"])
                winner = "home" if home_won else "away"
                result = "pass" if selection == "pass" else "win" if selection == winner else "loss"
                status = "settled"
            changed = database.execute(
                """
                UPDATE sports_ai_forecasts
                SET status=?,result=?,brier_score=?,settled_at=?
                WHERE id=? AND status='open'
                """,
                (status, result, brier, timestamp, row["id"]),
            )
            settled += max(changed.rowcount, 0)
    return settled


def refresh_sports(at: datetime | None = None) -> dict[str, Any]:
    current = (at or datetime.now(UTC)).astimezone(UTC)
    counts: dict[str, int] = {}
    history_counts: dict[str, int] = {}
    history_errors: dict[str, str] = {}
    player_counts: dict[str, dict[str, int]] = {}
    news_counts: dict[str, int] = {}
    news_errors: dict[str, str] = {}
    errors: dict[str, str] = {}
    odds_counts: dict[str, int] = {}
    cached_odds_counts: dict[str, int] = {}
    odds_slots: dict[str, str] = {}
    odds_errors: dict[str, str] = {}
    quota: Quota | None = None
    reported_quota: Quota | None = None
    quota_error: str | None = None
    try:
        odds_config = OddsApiConfig.from_env()
    except (TypeError, ValueError) as exc:
        odds_config = OddsApiConfig(api_key="", enabled=False)
        quota_error = str(exc)[:240]
    if odds_config.active:
        reported_quota = last_recorded_quota()
    for league in LEAGUES:
        try:
            events = fetch_league(league, at)
            if odds_config.active:
                clear_event_odds(events)
                decision = refresh_decision(
                    league,
                    events,
                    current,
                    retry_seconds=odds_config.retry_seconds,
                )
                if decision:
                    odds_slots[league] = decision.slot
                    if quota is None and quota_error is None:
                        try:
                            quota = probe_quota(odds_config)
                            reported_quota = quota
                        except OddsApiError as exc:
                            quota_error = str(exc)[:240]
                    if quota is not None and can_spend(odds_config, quota):
                        try:
                            result = fetch_moneylines(odds_config, league)
                            quota = result.quota
                            reported_quota = quota
                            odds_counts[league] = apply_moneylines(events, result.moneylines)
                            mark_refresh_attempt(
                                decision,
                                successful=result.quota.last > 0,
                                at=current,
                            )
                        except OddsApiError as exc:
                            odds_errors[league] = str(exc)[:240]
                            if exc.quota is not None:
                                quota = exc.quota
                                reported_quota = quota
                            mark_refresh_attempt(
                                decision,
                                successful=bool(exc.quota and exc.quota.last > 0),
                                at=current,
                            )
                    elif quota_error:
                        odds_errors[league] = quota_error
                        mark_refresh_attempt(decision, successful=False, at=current)
                    else:
                        odds_errors[league] = "Monthly odds budget is paused at its safe limit."
                        mark_refresh_attempt(decision, successful=False, at=current)
                cached_odds_counts[league] = _apply_cached_moneylines(events)
            counts[league] = store_events(events, observed_at=current)
            player_counts[league] = collect_player_appearances(events)
        except Exception as exc:
            errors[league] = str(exc)[:240]
            continue
        try:
            history_events = fetch_league_history_chunk(league, at)
            history_counts[league] = store_events(history_events, observed_at=current)
            stored_players = collect_stored_player_appearances(league)
            player_counts[league] = {
                key: int(player_counts[league].get(key, 0)) + int(stored_players.get(key, 0))
                for key in {"events", "players", "errors"}
            }
        except Exception as exc:
            history_errors[league] = str(exc)[:240]
        promoted_event_ids = [
            str(event["id"])
            for event in events
            if predict_event(event).get("signal") in PROMOTED_SIGNALS
        ]
        if promoted_event_ids:
            try:
                news_counts[league] = store_news_articles(
                    fetch_league_news(league, events, at),
                    replace_event_ids=[str(event["id"]) for event in events],
                )
            except Exception as exc:
                news_errors[league] = str(exc)[:240]
    settled = settle_picks()
    ai_settled = settle_sports_ai_forecasts()
    return {
        "counts": counts,
        "history_counts": history_counts,
        "history_errors": history_errors,
        "player_counts": player_counts,
        "news_counts": news_counts,
        "news_errors": news_errors,
        "errors": errors,
        "odds_api": {
            "enabled": odds_config.active,
            "working_limit": odds_config.working_limit,
            "reserve": odds_config.reserve_credits,
            "credits_used": reported_quota.used if reported_quota else None,
            "credits_remaining": reported_quota.remaining if reported_quota else None,
            "fresh_matches": odds_counts,
            "cached_matches": cached_odds_counts,
            "slots": odds_slots,
            "errors": odds_errors,
            "quota_error": quota_error,
        },
        "settled": settled,
        "ai_settled": ai_settled,
        "model": MODEL_VERSION,
    }


def _latest_odds(database: Any, event_id: str) -> dict[str, Any] | None:
    row = database.execute(
        """
        SELECT * FROM sports_odds_snapshots WHERE event_id=?
        ORDER BY observed_at DESC,id DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    return _odds_item(row)


def _odds_item(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    sportsbook = str(item.get("sportsbook") or "").strip()
    provider = str(item.get("provider") or "").strip()
    if sportsbook and provider == ODDS_PROVIDER:
        item["source_label"] = f"{sportsbook} via The Odds API"
    else:
        item["source_label"] = sportsbook or provider or "Unknown source"
    return item


def _bookmaker_odds_item(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    sportsbook = str(item.get("sportsbook") or "Unknown")
    item["source_label"] = f"{sportsbook} via The Odds API"
    item["home_probability_pct"] = round(float(item["home_probability"]) * 100, 1)
    item["away_probability_pct"] = round(float(item["away_probability"]) * 100, 1)
    item["fresh_at"] = str(item.get("source_updated_at") or item.get("observed_at") or "")
    item.pop("latest_rank", None)
    return item


def _latest_bookmaker_rows(database: Any, event_ids: list[str]) -> list[Any]:
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    return database.execute(
        f"""
        SELECT * FROM (
            SELECT b.*,ROW_NUMBER() OVER(
                PARTITION BY event_id,sportsbook_key ORDER BY observed_at DESC,id DESC
            ) AS latest_rank
            FROM sports_bookmaker_odds b
            WHERE event_id IN ({placeholders}) AND provider=?
        ) latest
        WHERE latest_rank=1
        """,  # noqa: S608 - placeholders are generated, not user input
        (*event_ids, ODDS_PROVIDER),
    ).fetchall()


def _fresh_bookmaker_items(
    rows: list[Any], reference_time: datetime | None = None
) -> list[dict[str, Any]]:
    items = [item for row in rows if (item := _bookmaker_odds_item(row)) is not None]
    timed: list[tuple[dict[str, Any], datetime]] = []
    reference = (reference_time or datetime.now(UTC)).astimezone(UTC)
    for item in items:
        try:
            updated = _parse_time(item["fresh_at"])
        except (TypeError, ValueError):
            continue
        age = reference - updated
        if -BOOKMAKER_FUTURE_TOLERANCE <= age <= BOOKMAKER_MAX_AGE:
            timed.append((item, updated))
    if not timed:
        return []
    newest = max(updated for _item, updated in timed)
    return [item for item, updated in timed if newest - updated <= BOOKMAKER_FRESHNESS]


def _market_comparison(event: dict[str, Any], rows: list[Any]) -> dict[str, Any]:
    completed = bool(event.get("completed")) or str(event.get("status")) == "post"
    reference_time = _parse_time(event["start_time"]) if completed else datetime.now(UTC)
    books = _fresh_bookmaker_items(rows, reference_time)
    book_count = len(books)
    consensus_home = (
        float(median(book["home_probability"] for book in books))
        if book_count >= MIN_CONSENSUS_BOOKS
        else None
    )
    consensus_away = 1 - consensus_home if consensus_home is not None else None
    best_home = max(books, key=lambda book: int(book["home_odds"]), default=None)
    best_away = max(books, key=lambda book: int(book["away_odds"]), default=None)
    bovada = next(
        (book for book in books if book["sportsbook_key"] == BOVADA_BOOKMAKER_KEY),
        None,
    )
    other_books = [book for book in books if book["sportsbook_key"] != BOVADA_BOOKMAKER_KEY]
    other_home = (
        float(median(book["home_probability"] for book in other_books))
        if len(other_books) >= MIN_CONSENSUS_BOOKS
        else None
    )
    bovada_summary: dict[str, Any] | None = None
    if bovada is not None:
        bovada_summary = dict(bovada)
        if other_home is not None:
            home_divergence = (float(bovada["home_probability"]) - other_home) * 100
            side = "home" if home_divergence >= 0 else "away"
            divergence = abs(home_divergence)
            bovada_summary.update(
                {
                    "comparison_book_count": len(other_books),
                    "home_divergence_pct": round(home_divergence, 1),
                    "away_divergence_pct": round(-home_divergence, 1),
                    "divergence_pct": round(divergence, 1),
                    "divergence_side": side,
                    "divergence_team": str(event.get(f"{side}_abbreviation") or "—"),
                    "material": divergence >= 2.0,
                }
            )
    output_books: list[dict[str, Any]] = []
    for book in sorted(books, key=lambda item: str(item["sportsbook"]).lower()):
        item = dict(book)
        item["home_vs_consensus_pct"] = (
            round((float(item["home_probability"]) - consensus_home) * 100, 1)
            if consensus_home is not None
            else None
        )
        item["best_home"] = best_home is not None and item["id"] == best_home["id"]
        item["best_away"] = best_away is not None and item["id"] == best_away["id"]
        output_books.append(item)
    return {
        "book_count": book_count,
        "minimum_book_count": MIN_CONSENSUS_BOOKS,
        "enough_books": consensus_home is not None,
        "home_probability_pct": round(consensus_home * 100, 1)
        if consensus_home is not None
        else None,
        "away_probability_pct": round(consensus_away * 100, 1)
        if consensus_away is not None
        else None,
        "bovada": bovada_summary,
        "best_home": dict(best_home) if best_home else None,
        "best_away": dict(best_away) if best_away else None,
        "books": output_books,
    }


def _paper_moneyline(
    event: dict[str, Any],
    odds: dict[str, Any] | None,
    comparison: dict[str, Any],
    *,
    has_bookmaker_rows: bool,
) -> dict[str, Any] | None:
    """Choose a recent line for a paper Call, preferring Bovada when present."""

    bovada = comparison.get("bovada")
    if bovada:
        return dict(bovada)
    if has_bookmaker_rows and not comparison.get("books"):
        return None
    if not odds:
        return None
    completed = bool(event.get("completed")) or str(event.get("status")) == "post"
    reference = _parse_time(event["start_time"]) if completed else datetime.now(UTC)
    try:
        observed = _parse_time(str(odds.get("fresh_at") or odds.get("observed_at") or ""))
    except (TypeError, ValueError):
        return None
    age = reference - observed
    if not -BOOKMAKER_FUTURE_TOLERANCE <= age <= BOOKMAKER_MAX_AGE:
        return None
    return dict(odds)


def _latest_prediction(database: Any, event_id: str) -> dict[str, Any] | None:
    row = database.execute(
        """
        SELECT * FROM sports_predictions WHERE event_id=?
        ORDER BY observed_at DESC,id DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    return _prediction_item(row)


def _pregame_prediction(
    database: Any,
    event_id: str,
    start_time: str,
) -> dict[str, Any] | None:
    """Return the last forecast that existed before the game started."""

    row = database.execute(
        """
        SELECT * FROM sports_predictions
        WHERE event_id=? AND observed_at<=?
        ORDER BY observed_at DESC,id DESC LIMIT 1
        """,
        (event_id, start_time),
    ).fetchone()
    return _prediction_item(row)


def _odds_at_or_before(
    database: Any,
    event_id: str,
    observed_at: str,
) -> dict[str, Any] | None:
    row = database.execute(
        """
        SELECT * FROM sports_odds_snapshots
        WHERE event_id=? AND observed_at<=?
        ORDER BY observed_at DESC,id DESC LIMIT 1
        """,
        (event_id, observed_at),
    ).fetchone()
    return _odds_item(row)


def _prediction_item(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item.pop("history_rank", None)
    item.pop("latest_rank", None)
    item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
    item["risks"] = json.loads(item.pop("risks_json") or "[]")
    item["edge_pct"] = round(float(item["edge"]) * 100, 1) if item.get("edge") is not None else None
    return item


def _receipt_timing(start_time: str, observed_at: str) -> str:
    delta = _parse_time(start_time) - _parse_time(observed_at)
    minutes = max(0, int(delta.total_seconds() // 60))
    hours, minute_part = divmod(minutes, 60)
    if hours and minute_part:
        return f"{hours}h {minute_part}m before start"
    if hours:
        return f"{hours}h before start"
    if minutes:
        return f"{minutes}m before start"
    return "at the pregame cutoff"


def _prediction_receipt(
    event: dict[str, Any],
    prediction: dict[str, Any] | None,
    odds: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not prediction:
        return None
    start = _parse_time(event["start_time"])
    sealed = bool(
        event.get("completed")
        or event.get("status") != "pre"
        or start <= datetime.now(UTC)
    )
    home_probability = float(prediction["home_probability"])
    winner_side = "home" if home_probability >= 0.5 else "away"
    selection = str(prediction.get("selection") or "pass")
    outcome: dict[str, Any] | None = None
    if event.get("completed"):
        home_score = _number(event.get("home_score"))
        away_score = _number(event.get("away_score"))
        if home_score is not None and away_score is not None and home_score != away_score:
            actual_side = "home" if home_score > away_score else "away"
            outcome = {
                "actual_side": actual_side,
                "actual_abbreviation": str(event[f"{actual_side}_abbreviation"]),
                "model_result": "win" if winner_side == actual_side else "loss",
                "value_result": (
                    "pass" if selection == "pass" else "win" if selection == actual_side else "loss"
                ),
                "final_score": (
                    f"{event['away_abbreviation']} {_score_display(away_score)} – "
                    f"{event['home_abbreviation']} {_score_display(home_score)}"
                ),
            }
    receipt_id = str(prediction.get("input_hash") or prediction.get("id") or "")[:12].upper()
    return {
        "id": receipt_id,
        "sealed": sealed,
        "status_label": "SEALED PREGAME" if sealed else "LIVE PREGAME SNAPSHOT",
        "captured_at": str(prediction["observed_at"]),
        "timing_label": _receipt_timing(str(event["start_time"]), str(prediction["observed_at"])),
        "model_version": str(prediction["model_version"]),
        "quality": str(prediction["quality"]),
        "input_hash": str(prediction.get("input_hash") or ""),
        "selection": selection,
        "signal": str(prediction.get("signal") or "model only"),
        "edge_pct": prediction.get("edge_pct"),
        "winner_side": winner_side,
        "winner_abbreviation": str(event[f"{winner_side}_abbreviation"]),
        "winner_probability_pct": round(
            float(prediction[f"{winner_side}_probability"]) * 100,
            1,
        ),
        "odds_observed_at": str(odds.get("observed_at") or "") if odds else "",
        "sportsbook": str(odds.get("sportsbook") or "") if odds else "",
        "odds_provider": str(odds.get("provider") or "") if odds else "",
        "market_label": "Final pregame" if sealed else "Current",
        "included": list(prediction.get("evidence") or []),
        "excluded": list(prediction.get("risks") or []),
        "outcome": outcome,
    }


def _news_summary(database: Any, event_id: str) -> tuple[int, dict[str, Any] | None]:
    row = database.execute(
        """
        SELECT *,COUNT(*) OVER() AS event_news_count
        FROM sports_news_articles WHERE event_id=?
        ORDER BY published_at DESC,id DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if not row:
        return 0, None
    item = dict(row)
    count = int(item.pop("event_news_count"))
    return count, item


def _preferred_side(prediction: dict[str, Any]) -> str | None:
    home_market = prediction.get("home_market_probability")
    away_market = prediction.get("away_market_probability")
    if home_market is None or away_market is None:
        return None
    home_edge = float(prediction["home_probability"]) - float(home_market)
    away_edge = float(prediction["away_probability"]) - float(away_market)
    return "home" if home_edge >= away_edge else "away"


def _edge_sparkline(
    database: Any,
    event: dict[str, Any],
    prediction: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build a small, fixed-scale history for the latest preferred side."""

    if not prediction or prediction.get("edge") is None or not _preferred_side(prediction):
        return None
    rows = database.execute(
        """
        SELECT home_probability,away_probability,
               home_market_probability,away_market_probability,observed_at
        FROM sports_predictions
        WHERE event_id=? AND model_version=? AND observed_at<=?
        ORDER BY observed_at DESC,id DESC LIMIT 24
        """,
        (event["id"], prediction["model_version"], event["start_time"]),
    ).fetchall()
    return _edge_sparkline_from_rows(event, prediction, rows)


def _edge_sparkline_from_rows(
    event: dict[str, Any],
    prediction: dict[str, Any] | None,
    rows: list[Any],
) -> dict[str, Any] | None:
    if (
        not prediction
        or prediction.get("edge") is None
        or not (side := _preferred_side(prediction))
    ):
        return None
    points: list[dict[str, Any]] = []
    for row in reversed(rows):
        model_probability = row[f"{side}_probability"]
        market_probability = row[f"{side}_market_probability"]
        if model_probability is None or market_probability is None:
            continue
        points.append(
            {
                "observed_at": str(row["observed_at"]),
                "edge_pct": round(
                    (float(model_probability) - float(market_probability)) * 100,
                    1,
                ),
            }
        )
    if not points:
        return None

    # A fixed +/-12 point scale keeps small moves from looking dramatic.
    chart_width = 92.0
    chart_midline = 12.0
    chart_range = 9.0
    coordinates: list[str] = []
    observed_times = [_parse_time(point["observed_at"]) for point in points]
    time_span = (observed_times[-1] - observed_times[0]).total_seconds()
    for point, observed_time in zip(points, observed_times, strict=True):
        x = (
            chart_width / 2
            if time_span <= 0
            else 2 + (observed_time - observed_times[0]).total_seconds() * 88 / time_span
        )
        clipped_edge = max(-12.0, min(12.0, float(point["edge_pct"])))
        y = chart_midline - (clipped_edge / 12.0) * chart_range
        coordinates.append(f"{x:.1f},{y:.1f}")

    start = float(points[0]["edge_pct"])
    current = float(points[-1]["edge_pct"])
    change = round(current - start, 1)
    team = str(event[f"{side}_abbreviation"])
    if len(points) == 1:
        movement = "is new"
    elif change > 0.2:
        movement = f"grew {change:.1f} points"
    elif change < -0.2:
        movement = f"fell {abs(change):.1f} points"
    else:
        movement = "was steady"
    return {
        "side": side,
        "team": team,
        "points": points,
        "plot_points": " ".join(coordinates),
        "dot_x": coordinates[-1].split(",", 1)[0],
        "dot_y": coordinates[-1].split(",", 1)[1],
        "current_pct": current,
        "change_pct": change,
        "label": f"{team} model edge {movement}; now {current:+.1f} percentage points",
    }


def _event_attention(event: dict[str, Any]) -> dict[str, Any]:
    """Turn a stored prediction into the compact, ranked Sports Pulse contract."""

    prediction = event.get("prediction") or {}
    odds = event.get("odds") or {}
    side = str(prediction.get("selection") or "")
    if side not in {"home", "away"}:
        side = (
            "home"
            if float(prediction.get("home_probability") or 0.5)
            >= float(prediction.get("away_probability") or 0.5)
            else "away"
        )
    other_side = "away" if side == "home" else "home"
    team_id = str(event.get(f"{side}_team_id") or "")
    abbreviation = str(event.get(f"{side}_abbreviation") or "—")
    coin_seed = f"{team_id}:{abbreviation}".encode()
    coin_tone = int(hashlib.sha256(coin_seed).hexdigest()[:2], 16) % 5
    model_probability = _number(prediction.get(f"{side}_probability"))
    market_probability = _number(prediction.get(f"{side}_market_probability"))
    if market_probability is None:
        current_home, current_away = no_vig_probabilities(
            odds.get("home_odds"), odds.get("away_odds")
        )
        market_probability = current_home if side == "home" else current_away
    open_home, open_away = no_vig_probabilities(
        odds.get("home_open_odds"), odds.get("away_open_odds")
    )
    open_market = open_home if side == "home" else open_away
    market_move_pct = (
        round((market_probability - open_market) * 100, 1)
        if market_probability is not None and open_market is not None
        else None
    )
    edge_pct = _number(prediction.get("edge_pct"))
    signal = str(prediction.get("signal") or "model only")
    if signal in PROMOTED_SIGNALS and edge_pct is not None:
        reason = f"Model is {abs(edge_pct):.1f} points above market on {abbreviation}."
    elif market_move_pct is not None and abs(market_move_pct) >= 0.5:
        direction = "toward" if market_move_pct > 0 else "away from"
        reason = f"Market moved {abs(market_move_pct):.1f} points {direction} {abbreviation}."
    elif event.get("status") == "in":
        reason = f"{event.get('status_detail') or 'Live'} · score and market are moving."
    else:
        reason = "No verified model-versus-market gap yet."
    history = event.get("edge_history") or {}
    model_change_pct = _number(history.get("change_pct")) or 0.0
    signal_rank = (
        3
        if signal == "watch"
        else 2
        if signal == "lean"
        else 1
        if event.get("status") == "in"
        else 0
    )
    attention_rank = (
        signal_rank * 1000 + abs(float(edge_pct or 0)) * 10 + abs(float(market_move_pct or 0))
    )

    home_probability = float(prediction.get("home_probability") or 0.5)
    away_probability = float(prediction.get("away_probability") or 0.5)
    winner_side = "home" if home_probability >= away_probability else "away"
    winner_opponent_side = "away" if winner_side == "home" else "home"
    winner_team_id = str(event.get(f"{winner_side}_team_id") or "")
    winner_abbreviation = str(event.get(f"{winner_side}_abbreviation") or "—")
    winner_seed = f"{winner_team_id}:{winner_abbreviation}".encode()
    winner_coin_tone = int(hashlib.sha256(winner_seed).hexdigest()[:2], 16) % 5

    score_model = SCORE_MODELS.get(str(event.get("league")), SCORE_MODELS["mlb"])
    market_total = _number(odds.get("total"))
    projected_total = market_total or float(score_model["total"])
    exponent = float(score_model["exponent"])
    safe_home_probability = max(0.05, min(0.95, home_probability))
    score_ratio = (safe_home_probability / (1 - safe_home_probability)) ** (1 / exponent)
    projected_home_score = projected_total * score_ratio / (1 + score_ratio)
    projected_away_score = projected_total - projected_home_score
    score_decimals = int(score_model["decimals"])
    bovada = (event.get("market_comparison") or {}).get("bovada") or {}

    return {
        "signal_side": side,
        "signal_team_id": team_id,
        "signal_team_name": str(event.get(f"{side}_team_name") or "Unknown"),
        "signal_abbreviation": abbreviation,
        "signal_coin_tone": coin_tone,
        "opponent_abbreviation": str(event.get(f"{other_side}_abbreviation") or "—"),
        "signal_record": event.get(f"{side}_record"),
        "model_probability_pct": (
            round(model_probability * 100, 1) if model_probability is not None else None
        ),
        "market_probability_pct": (
            round(market_probability * 100, 1) if market_probability is not None else None
        ),
        "market_move_pct": market_move_pct,
        "model_change_pct": round(model_change_pct, 1),
        "signal_reason": reason,
        "attention_rank": round(attention_rank, 2),
        "model_winner_side": winner_side,
        "model_winner_team_id": winner_team_id,
        "model_winner_team_name": str(event.get(f"{winner_side}_team_name") or "Unknown"),
        "model_winner_abbreviation": winner_abbreviation,
        "model_winner_record": event.get(f"{winner_side}_record"),
        "model_winner_opponent_team_name": str(
            event.get(f"{winner_opponent_side}_team_name") or "Unknown"
        ),
        "model_winner_opponent_abbreviation": str(
            event.get(f"{winner_opponent_side}_abbreviation") or "—"
        ),
        "model_winner_probability_pct": round(
            (home_probability if winner_side == "home" else away_probability) * 100,
            1,
        ),
        "model_winner_coin_tone": winner_coin_tone,
        "projected_home_score": round(projected_home_score, score_decimals),
        "projected_away_score": round(projected_away_score, score_decimals),
        "projected_home_score_display": f"{projected_home_score:.{score_decimals}f}",
        "projected_away_score_display": f"{projected_away_score:.{score_decimals}f}",
        "model_winner_projected_score_display": (
            f"{projected_home_score:.{score_decimals}f}"
            if winner_side == "home"
            else f"{projected_away_score:.{score_decimals}f}"
        ),
        "model_winner_opponent_projected_score_display": (
            f"{projected_away_score:.{score_decimals}f}"
            if winner_side == "home"
            else f"{projected_home_score:.{score_decimals}f}"
        ),
        "projected_score_basis": "market total" if market_total else "league baseline",
        "bovada_divergence_pct": bovada.get("divergence_pct"),
        "bovada_divergence_team": bovada.get("divergence_team"),
        "bovada_divergence_material": bool(bovada.get("material")),
    }


def _event_row(database: Any, row: Any) -> dict[str, Any]:
    item = dict(row)
    item["odds"] = _latest_odds(database, str(item["id"]))
    bookmaker_rows = _latest_bookmaker_rows(database, [str(item["id"])])
    item["market_comparison"] = _market_comparison(item, bookmaker_rows)
    item["bovada_odds"] = item["market_comparison"].get("bovada")
    item["paper_odds"] = _paper_moneyline(
        item,
        item["odds"],
        item["market_comparison"],
        has_bookmaker_rows=bool(bookmaker_rows),
    )
    item["prediction"] = _latest_prediction(database, str(item["id"]))
    item["edge_history"] = _edge_sparkline(database, item, item["prediction"])
    item["news_count"], item["latest_news"] = _news_summary(database, str(item["id"]))
    item["was_promoted"] = (
        database.execute(
            """
            SELECT 1 FROM sports_predictions
            WHERE event_id=? AND signal IN ('lean','watch') LIMIT 1
            """,
            (item["id"],),
        ).fetchone()
        is not None
    )
    item.update(_event_attention(item))
    return item


def _event_rows(database: Any, rows: list[Any]) -> list[dict[str, Any]]:
    """Build slate rows with a fixed number of database queries."""

    events = [dict(row) for row in rows]
    event_ids = [str(event["id"]) for event in events]
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    parameters = tuple(event_ids)

    odds_rows = database.execute(
        f"""
        SELECT * FROM (
            SELECT o.*,ROW_NUMBER() OVER(
                PARTITION BY event_id ORDER BY observed_at DESC,id DESC
            ) AS latest_rank
            FROM sports_odds_snapshots o
            WHERE event_id IN ({placeholders})
        ) latest
        WHERE latest_rank=1
        """,  # noqa: S608 - placeholders are generated, not user input
        parameters,
    ).fetchall()
    odds_by_event: dict[str, dict[str, Any]] = {}
    for row in odds_rows:
        item = _odds_item(row)
        assert item is not None
        item.pop("latest_rank", None)
        odds_by_event[str(item["event_id"])] = item

    bookmaker_rows = _latest_bookmaker_rows(database, event_ids)
    bookmakers_by_event: dict[str, list[Any]] = defaultdict(list)
    for row in bookmaker_rows:
        bookmakers_by_event[str(row["event_id"])].append(row)

    prediction_rows = database.execute(
        f"""
        WITH latest_models AS (
            SELECT event_id,model_version FROM (
                SELECT event_id,model_version,ROW_NUMBER() OVER(
                    PARTITION BY event_id ORDER BY observed_at DESC,id DESC
                ) AS latest_rank
                FROM sports_predictions
                WHERE event_id IN ({placeholders})
            ) latest
            WHERE latest_rank=1
        ), ranked AS (
            SELECT p.*,ROW_NUMBER() OVER(
                PARTITION BY p.event_id ORDER BY p.observed_at DESC,p.id DESC
            ) AS history_rank
            FROM sports_predictions p
            JOIN latest_models latest
              ON latest.event_id=p.event_id AND latest.model_version=p.model_version
        )
        SELECT * FROM ranked
        WHERE history_rank<=24
        ORDER BY event_id,observed_at DESC,id DESC
        """,  # noqa: S608 - placeholders are generated, not user input
        parameters,
    ).fetchall()
    prediction_history: dict[str, list[Any]] = defaultdict(list)
    for row in prediction_rows:
        prediction_history[str(row["event_id"])].append(row)
    predictions_by_event = {
        event_id: _prediction_item(history[0])
        for event_id, history in prediction_history.items()
        if history
    }

    news_rows = database.execute(
        f"""
        SELECT * FROM (
            SELECT n.*,COUNT(*) OVER(PARTITION BY event_id) AS event_news_count,
                   ROW_NUMBER() OVER(
                       PARTITION BY event_id ORDER BY published_at DESC,id DESC
                   ) AS latest_rank
            FROM sports_news_articles n
            WHERE event_id IN ({placeholders})
        ) latest
        WHERE latest_rank=1
        """,  # noqa: S608 - placeholders are generated, not user input
        parameters,
    ).fetchall()
    news_by_event: dict[str, tuple[int, dict[str, Any]]] = {}
    for row in news_rows:
        item = dict(row)
        count = int(item.pop("event_news_count"))
        item.pop("latest_rank", None)
        news_by_event[str(item["event_id"])] = (count, item)

    promoted_rows = database.execute(
        f"""
        SELECT DISTINCT event_id FROM sports_predictions
        WHERE event_id IN ({placeholders}) AND signal IN ('lean','watch')
        """,  # noqa: S608 - placeholders are generated, not user input
        parameters,
    ).fetchall()
    promoted_ids = {str(row["event_id"]) for row in promoted_rows}

    output: list[dict[str, Any]] = []
    for event in events:
        event_id = str(event["id"])
        prediction = predictions_by_event.get(event_id)
        event["odds"] = odds_by_event.get(event_id)
        event_bookmaker_rows = bookmakers_by_event.get(event_id, [])
        event["market_comparison"] = _market_comparison(
            event,
            event_bookmaker_rows,
        )
        event["bovada_odds"] = event["market_comparison"].get("bovada")
        event["paper_odds"] = _paper_moneyline(
            event,
            event["odds"],
            event["market_comparison"],
            has_bookmaker_rows=bool(event_bookmaker_rows),
        )
        event["prediction"] = prediction
        event["edge_history"] = _edge_sparkline_from_rows(
            event,
            prediction,
            prediction_history.get(event_id, []),
        )
        event["news_count"], event["latest_news"] = news_by_event.get(event_id, (0, None))
        event["was_promoted"] = event_id in promoted_ids
        event.update(_event_attention(event))
        output.append(event)
    return output


def sports_slate(league: str = "all", limit: int = 80) -> dict[str, Any]:
    current = datetime.now(UTC)
    parameters: list[Any] = [
        _iso(current - timedelta(hours=12)),
        _iso(current + timedelta(days=4)),
    ]
    league_filter = ""
    if league in LEAGUES:
        league_filter = " AND league=?"
        parameters.append(league)
    parameters.append(max(1, min(limit, 200)))
    with connection() as database:
        rows = database.execute(
            f"""
            SELECT * FROM sports_events
            WHERE start_time>=? AND start_time<=?{league_filter}
            ORDER BY CASE status WHEN 'in' THEN 0 WHEN 'pre' THEN 1 ELSE 2 END,
                     start_time,id LIMIT ?
            """,  # noqa: S608 - filter is a fixed internal fragment
            tuple(parameters),
        ).fetchall()
        events = _event_rows(database, rows)
        last_run = database.execute(
            """
            SELECT status,finished_at,error FROM ingestion_runs
            WHERE source=? AND feed=? ORDER BY finished_at DESC LIMIT 1
            """,
            (SOURCE, FEED),
        ).fetchone()
    return {
        "events": events,
        "leagues": [{"key": key, "name": value["name"]} for key, value in LEAGUES.items()],
        "league": league,
        "updated_at": str(last_run["finished_at"]) if last_run else None,
        "source_status": str(last_run["status"]) if last_run else "waiting",
        "source_error": str(last_run["error"] or "") if last_run else "",
        "model": MODEL_VERSION,
    }


def _sports_series_key(event: dict[str, Any]) -> str:
    team_ids = sorted(
        (str(event.get("away_team_id") or ""), str(event.get("home_team_id") or ""))
    )
    return f"{event.get('league')}:{':'.join(team_ids)}"


def _group_sports_series(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        series[_sports_series_key(event)].append(event)
    grouped: list[dict[str, Any]] = []
    used: set[str] = set()
    for event in events:
        series_key = _sports_series_key(event)
        if series_key in used:
            continue
        used.add(series_key)
        lead = dict(event)
        more = [item for item in series[series_key] if item["id"] != event["id"]]
        lead.update(
            series_key=series_key,
            series_game_count=len(series[series_key]),
            series_more_count=len(more),
            series_more=more,
        )
        grouped.append(lead)
    return grouped


def sports_pulse(league: str = "all", view: str = "signals", limit: int = 30) -> dict[str, Any]:
    """Return promoted signals ranked first, with repeated series kept together."""

    _ = view  # Keep the old query parameter harmless while the slate view is hidden.
    payload = sports_slate(league, limit=200)
    available = [event for event in payload["events"] if str(event.get("status")) in {"pre", "in"}]
    signals = [
        event
        for event in available
        if event.get("status") == "pre"
        and str((event.get("prediction") or {}).get("signal")) in PROMOTED_SIGNALS
    ]
    signals.sort(
        key=lambda event: (
            -float(event.get("attention_rank") or 0),
            str(event.get("start_time") or ""),
            str(event.get("id") or ""),
        )
    )
    grouped = _group_sports_series(signals)

    shown = grouped[: max(1, min(limit, 100))]
    return {
        **payload,
        "events": shown,
        "model_record": _model_alpha(league),
        "view": "signals",
        "signal_count": len(signals),
        "display_count": len(shown),
        "scanned_count": len(available),
        "hidden_count": max(0, len(available) - len(signals)),
    }


def sports_radar(league: str = "all", limit: int = 40) -> dict[str, Any]:
    """Return live games and material changes attached to promoted Pulse signals."""

    payload = sports_slate(league, limit=200)
    changes: list[dict[str, Any]] = []
    for event in payload["events"]:
        if event.get("status") not in {"pre", "in"}:
            continue
        prediction = event.get("prediction") or {}
        signal = str(prediction.get("signal") or "")
        market_move = float(event.get("market_move_pct") or 0)
        model_move = float(event.get("model_change_pct") or 0)
        if event.get("status") == "in" and event.get("was_promoted"):
            item = dict(event)
            item.update(
                radar_kind="live",
                radar_label="LIVE",
                radar_value=0.0,
                radar_detail=str(event.get("status_detail") or "Game in progress"),
            )
        elif signal in PROMOTED_SIGNALS and abs(market_move) >= 0.5:
            direction = "toward" if market_move > 0 else "away from"
            item = dict(event)
            item.update(
                radar_kind="market",
                radar_label="PRICE",
                radar_value=round(market_move, 1),
                radar_detail=(
                    f"Market moved {abs(market_move):.1f} points {direction} "
                    f"{event['signal_abbreviation']} since open."
                ),
            )
        elif signal in PROMOTED_SIGNALS and abs(model_move) >= 0.5:
            direction = "strengthened" if model_move > 0 else "weakened"
            item = dict(event)
            item.update(
                radar_kind="edge",
                radar_label="EDGE",
                radar_value=round(model_move, 1),
                radar_detail=(
                    f"{event['signal_abbreviation']} model-versus-market edge {direction} by "
                    f"{abs(model_move):.1f} points."
                ),
            )
        else:
            continue
        changes.append(item)
    changes.sort(
        key=lambda event: (
            0 if event["radar_kind"] == "live" else 1,
            -abs(float(event.get("radar_value") or 0)),
            str(event.get("start_time") or ""),
        )
    )
    grouped = _group_sports_series(changes)
    return {
        **payload,
        "events": grouped[: max(1, min(limit, 100))],
        "change_count": len(changes),
        "display_count": len(grouped),
        "tracked_count": len(
            {
                _sports_series_key(event)
                for event in payload["events"]
                if event.get("status") in {"pre", "in"} and event.get("was_promoted")
            }
        ),
    }


def _rate_history_points(
    outcomes: list[tuple[str, bool]],
    current_record: tuple[int, int] | None = None,
) -> list[dict[str, Any]]:
    recent = outcomes[-ALPHA_HISTORY_POINTS:]
    if current_record is None:
        running_wins = running_losses = 0
        points: list[dict[str, Any]] = []
        for observed_at, won in recent:
            running_wins += int(won)
            running_losses += int(not won)
            points.append(
                {
                    "at": observed_at,
                    "rate": round(
                        running_wins / (running_wins + running_losses) * 100,
                        1,
                    ),
                }
            )
        return points
    wins, losses = current_record
    previous_wins, previous_losses = wins, losses
    for _, won in reversed(recent):
        if won and previous_wins > 0:
            previous_wins -= 1
        elif not won and previous_losses > 0:
            previous_losses -= 1
    points = []
    if previous_wins + previous_losses:
        points.append(
            {
                "at": recent[0][0] if recent else "",
                "rate": round(
                    previous_wins / (previous_wins + previous_losses) * 100,
                    1,
                ),
            }
        )
    for observed_at, won in recent:
        previous_wins += int(won)
        previous_losses += int(not won)
        points.append(
            {
                "at": observed_at,
                "rate": round(
                    previous_wins / (previous_wins + previous_losses) * 100,
                    1,
                ),
            }
        )
    return points or (
        [{"at": "", "rate": round(wins / (wins + losses) * 100, 1)}] if wins + losses else []
    )


def _rate_history(
    outcomes: list[tuple[str, bool]], current_record: tuple[int, int] | None = None
) -> list[float]:
    return [point["rate"] for point in _rate_history_points(outcomes, current_record)]


def _american_profit(odds: int | None) -> float | None:
    if odds is None or odds == 0:
        return None
    return odds / 100 if odds > 0 else 100 / abs(odds)


def _sample_assessment(games: int) -> dict[str, Any]:
    if games < 25:
        label = "VERY EARLY SAMPLE"
        message = (
            "Too few settled forecasts to judge the model. "
            "These numbers are a receipt, not proof."
        )
    elif games < 100:
        label = "EARLY SAMPLE"
        message = "Results can still move sharply. Treat every rate as descriptive, not predictive."
    elif games < MODEL_SCORECARD_TARGET:
        label = "BUILDING SAMPLE"
        message = "The record is becoming useful, but it has not reached the public review target."
    else:
        label = "REVIEWABLE SAMPLE"
        message = (
            "The scorecard has reached the public review target. "
            "Calibration still matters most."
        )
    return {
        "label": label,
        "message": message,
        "target": MODEL_SCORECARD_TARGET,
        "remaining": max(0, MODEL_SCORECARD_TARGET - games),
        "progress_pct": round(min(1, games / MODEL_SCORECARD_TARGET) * 100, 1),
    }


def _model_alpha(league: str) -> dict[str, Any]:
    cache_key = (str(runner_db.DATABASE_PATH), league)
    current = time.monotonic()
    with _MODEL_RECORD_CACHE_LOCK:
        cached = _MODEL_RECORD_CACHE.get(cache_key)
        if cached and cached[0] > current:
            return cached[1]
    result = _build_model_alpha(league)
    with _MODEL_RECORD_CACHE_LOCK:
        _MODEL_RECORD_CACHE[cache_key] = (
            current + MODEL_RECORD_CACHE_TTL_SECONDS,
            result,
        )
    return result


def _build_model_alpha(league: str) -> dict[str, Any]:
    parameters: list[Any] = [_iso(datetime.now(UTC) - timedelta(days=HISTORY_TARGET_DAYS))]
    league_filter = ""
    if league in LEAGUES:
        league_filter = " AND e.league=?"
        parameters.append(league)
    with connection() as database:
        rows = database.execute(
            f"""
            SELECT e.id,e.league,e.start_time,e.home_score,e.away_score,
                   e.home_team_name,e.home_abbreviation,e.away_team_name,e.away_abbreviation,
                   p.id AS prediction_id,p.model_version,p.input_hash,
                   p.observed_at AS prediction_observed_at,
                   p.home_probability,p.away_probability,p.selection,p.signal,p.edge,
                   ep.id AS edge_prediction_id,ep.observed_at AS edge_observed_at,
                   ep.selection AS edge_selection,ep.signal AS edge_signal,ep.edge AS edge_value,
                   ep.home_market_probability AS edge_home_market_probability,
                   ep.away_market_probability AS edge_away_market_probability,
                   eo.home_odds AS edge_home_odds,eo.away_odds AS edge_away_odds,
                   eo.sportsbook AS edge_sportsbook,
                   co.home_odds AS close_home_odds,co.away_odds AS close_away_odds
            FROM sports_events e
            JOIN sports_predictions p ON p.id=(
                SELECT p2.id FROM sports_predictions p2
                WHERE p2.event_id=e.id AND p2.observed_at<=e.start_time
                ORDER BY p2.observed_at DESC,p2.id DESC LIMIT 1
            )
            LEFT JOIN sports_predictions ep ON ep.id=(
                SELECT p3.id FROM sports_predictions p3
                WHERE p3.event_id=e.id AND p3.model_version=p.model_version
                  AND p3.observed_at<=e.start_time
                  AND p3.signal IN ('lean','watch')
                  AND p3.selection IN ('home','away')
                ORDER BY p3.observed_at,p3.id LIMIT 1
            )
            LEFT JOIN sports_odds_snapshots eo ON eo.id=(
                SELECT o1.id FROM sports_odds_snapshots o1
                WHERE o1.event_id=e.id AND ep.observed_at IS NOT NULL
                  AND o1.observed_at<=ep.observed_at
                ORDER BY o1.observed_at DESC,o1.id DESC LIMIT 1
            )
            LEFT JOIN sports_odds_snapshots co ON co.id=(
                SELECT o2.id FROM sports_odds_snapshots o2
                WHERE o2.event_id=e.id AND o2.observed_at<=e.start_time
                ORDER BY o2.observed_at DESC,o2.id DESC LIMIT 1
            )
            WHERE e.completed=1
              AND e.start_time>=?
              AND e.season_type NOT IN ('preseason','pre-season'){league_filter}
            ORDER BY e.start_time,e.id
            """,  # noqa: S608 - filter is a fixed internal fragment
            tuple(parameters),
        ).fetchall()

    all_rows = [dict(row) for row in rows]
    current_rows = [row for row in all_rows if str(row["model_version"]) == MODEL_VERSION]
    evaluated: list[dict[str, Any]] = []
    edge_calls = edge_wins = edge_graded = 0
    edge_return_units = 0.0
    confidence_buckets: dict[str, list[tuple[bool, float]]] = defaultdict(list)
    edge_buckets: dict[str, list[tuple[bool, float | None]]] = defaultdict(list)
    league_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    clv_values: list[float] = []
    correct = 0
    brier_total = 0.0
    history_points: list[dict[str, Any]] = []
    receipt_rows: list[dict[str, Any]] = []
    for row in current_rows:
        home_score = _number(row.get("home_score"))
        away_score = _number(row.get("away_score"))
        if home_score is None or away_score is None or home_score == away_score:
            continue
        home_won = home_score > away_score
        home_probability = float(row["home_probability"])
        winner_side = "home" if home_probability >= 0.5 else "away"
        model_won = home_won if winner_side == "home" else not home_won
        correct += int(model_won)
        brier = (home_probability - int(home_won)) ** 2
        brier_total += brier
        confidence = max(home_probability, 1 - home_probability)
        confidence_label = (
            "50–55%" if confidence < 0.55 else "55–60%" if confidence < 0.60 else "60%+"
        )
        confidence_buckets[confidence_label].append((model_won, confidence))

        edge_selection = str(row.get("edge_selection") or "pass")
        edge_result: str | None = None
        edge_odds: int | None = None
        edge_clv: float | None = None
        if row.get("edge_prediction_id") and edge_selection in {"home", "away"}:
            edge_calls += 1
            selected_won = home_won if edge_selection == "home" else not home_won
            edge_wins += int(selected_won)
            edge_result = "win" if selected_won else "loss"
            edge_odds = (
                row.get("edge_home_odds")
                if edge_selection == "home"
                else row.get("edge_away_odds")
            )
            profit = _american_profit(int(edge_odds)) if edge_odds is not None else None
            if profit is None:
                result_units = None
            else:
                result_units = profit if selected_won else -1.0
            if result_units is not None:
                edge_graded += 1
                edge_return_units += result_units
            close_home, close_away = no_vig_probabilities(
                row.get("close_home_odds"), row.get("close_away_odds")
            )
            entry_market = _number(row.get(f"edge_{edge_selection}_market_probability"))
            close_market = close_home if edge_selection == "home" else close_away
            if entry_market is not None and close_market is not None:
                edge_clv = round((close_market - entry_market) * 100, 2)
                clv_values.append(edge_clv)
            edge_size = abs(float(row.get("edge_value") or 0)) * 100
            edge_label = "2–5 pp" if edge_size < 5 else "5–8 pp" if edge_size < 8 else "8+ pp"
            edge_buckets[edge_label].append((selected_won, result_units))

        row["model_won"] = model_won
        row["brier"] = brier
        evaluated.append(row)
        league_groups[str(row["league"])].append(row)
        history_points.append(
            {
                "at": str(row["start_time"]),
                "rate": round(correct / len(evaluated) * 100, 1),
            }
        )
        receipt_rows.append(
            {
                "id": str(row["id"]),
                "league": str(row["league"]),
                "start_time": str(row["start_time"]),
                "away_abbreviation": str(row["away_abbreviation"]),
                "home_abbreviation": str(row["home_abbreviation"]),
                "away_score": _score_display(away_score),
                "home_score": _score_display(home_score),
                "winner_abbreviation": str(row[f"{winner_side}_abbreviation"]),
                "winner_probability_pct": round(
                    float(row[f"{winner_side}_probability"]) * 100,
                    1,
                ),
                "model_result": "win" if model_won else "loss",
                "signal": str(row.get("edge_signal") or "pass"),
                "selection_abbreviation": (
                    str(row[f"{edge_selection}_abbreviation"])
                    if edge_selection in {"home", "away"}
                    else "PASS"
                ),
                "edge_pct": (
                    round(float(row["edge_value"]) * 100, 1)
                    if row.get("edge_value") is not None
                    else None
                ),
                "edge_result": edge_result,
                "edge_odds": int(edge_odds) if edge_odds is not None else None,
                "edge_clv_pct": edge_clv,
                "receipt_id": str(row.get("input_hash") or "")[:12].upper(),
                "captured_at": str(row["prediction_observed_at"]),
            }
        )

    sample = len(evaluated)
    calibration = []
    for label in ("50–55%", "55–60%", "60%+"):
        results = confidence_buckets.get(label)
        if not results:
            continue
        expected = sum(confidence for _, confidence in results) / len(results) * 100
        actual = sum(won for won, _ in results) / len(results) * 100
        calibration.append(
            {
                "label": label,
                "games": len(results),
                "expected": round(expected, 1),
                "hit_rate": round(actual, 1),
                "gap_pct": round(actual - expected, 1),
            }
        )

    edge_breakdown = []
    for label in ("2–5 pp", "5–8 pp", "8+ pp"):
        results = edge_buckets.get(label)
        if not results:
            continue
        graded = [units for _, units in results if units is not None]
        edge_breakdown.append(
            {
                "label": label,
                "calls": len(results),
                "wins": sum(won for won, _ in results),
                "roi": round(sum(graded) / len(graded) * 100, 1) if graded else None,
            }
        )

    league_breakdown = []
    for league_key in LEAGUES:
        league_rows = league_groups.get(league_key)
        if not league_rows:
            continue
        league_correct = sum(bool(row["model_won"]) for row in league_rows)
        league_breakdown.append(
            {
                "league": league_key,
                "games": len(league_rows),
                "accuracy": round(league_correct / len(league_rows) * 100, 1),
                "brier": round(
                    sum(float(row["brier"]) for row in league_rows) / len(league_rows),
                    3,
                ),
            }
        )

    return {
        "version": MODEL_VERSION,
        "games": sample,
        "wins": correct,
        "losses": sample - correct,
        "accuracy": round(correct / sample * 100, 1) if sample else None,
        "brier": round(brier_total / sample, 3) if sample else None,
        "edge_calls": edge_calls,
        "edge_wins": edge_wins,
        "edge_losses": edge_calls - edge_wins,
        "edge_accuracy": round(edge_wins / edge_calls * 100, 1) if edge_calls else None,
        "edge_graded": edge_graded,
        "edge_units": round(edge_return_units, 2) if edge_graded else None,
        "edge_roi": round(edge_return_units / edge_graded * 100, 1) if edge_graded else None,
        "average_clv_pct": round(sum(clv_values) / len(clv_values), 2) if clv_values else None,
        "clv_calls": len(clv_values),
        "history": [point["rate"] for point in history_points[-ALPHA_HISTORY_POINTS:]],
        "history_points": history_points[-ALPHA_HISTORY_POINTS:],
        "calibration": calibration,
        "edge_breakdown": edge_breakdown,
        "league_breakdown": league_breakdown,
        "receipts": list(reversed(receipt_rows[-40:])),
        "sample": _sample_assessment(sample),
    }


def sports_alpha(league: str = "all", limit: int = 24) -> dict[str, Any]:
    """Rank team and player results and keep the underlying rate history visible."""

    selected_league = league if league in LEAGUES else "all"
    result_limit = max(1, min(limit, 100))
    cutoff = _iso(datetime.now(UTC) - timedelta(days=HISTORY_TARGET_DAYS))
    coverage_parameters: tuple[Any, ...] = (selected_league,) if selected_league in LEAGUES else ()
    history_parameters: list[Any] = [cutoff]
    league_filter = ""
    player_league_filter = ""
    if coverage_parameters:
        league_filter = " AND league=?"
        player_league_filter = " AND a.league=?"
        history_parameters.append(selected_league)
    with connection() as database:
        event_rows = database.execute(
            f"""
            SELECT * FROM sports_events
            WHERE start_time>=?
              AND season_type NOT IN ('preseason','pre-season'){league_filter}
            ORDER BY start_time,id
            """,  # noqa: S608 - filter is a fixed internal fragment
            tuple(history_parameters),
        ).fetchall()
        coverage_row = database.execute(
            f"""
            SELECT COUNT(*) AS events,
                   SUM(CASE WHEN completed=1 THEN 1 ELSE 0 END) AS completed,
                   MIN(CASE WHEN completed=1 THEN start_time END) AS history_start,
                   MAX(CASE WHEN completed=1 THEN start_time END) AS history_end,
                   MAX(last_collected_at) AS updated_at
            FROM sports_events
            WHERE season_type NOT IN ('preseason','pre-season'){league_filter}
            """,  # noqa: S608 - filter is a fixed internal fragment
            coverage_parameters,
        ).fetchone()
        player_rows = database.execute(
            f"""
            WITH player_totals AS (
                SELECT a.league,a.player_id,MAX(a.player_name) AS player_name,
                       COUNT(*) AS games,SUM(a.won) AS wins
                FROM sports_player_appearances a
                JOIN sports_events e ON e.id=a.event_id
                WHERE e.start_time>=?{player_league_filter}
                GROUP BY a.league,a.player_id
                HAVING COUNT(*)>=3
            ),
            ranked_players AS (
                SELECT *,COUNT(*) OVER() AS eligible_player_count,
                       ROW_NUMBER() OVER(
                           ORDER BY (wins+2.0)/(games+4.0) DESC,games DESC,player_name
                       ) AS player_rank
                FROM player_totals
            ),
            selected_players AS (
                SELECT * FROM ranked_players WHERE player_rank<=?
            ),
            ranked_history AS (
                SELECT a.*,e.start_time,selected.eligible_player_count,
                       ROW_NUMBER() OVER(
                           PARTITION BY a.league,a.player_id
                           ORDER BY e.start_time DESC,a.event_id DESC
                       ) AS history_rank
                FROM sports_player_appearances a
                JOIN sports_events e ON e.id=a.event_id
                JOIN selected_players selected
                  ON selected.league=a.league AND selected.player_id=a.player_id
                WHERE e.start_time>=?{player_league_filter}
            )
            SELECT * FROM ranked_history
            WHERE history_rank<=?
            ORDER BY start_time,event_id,player_id
            """,  # noqa: S608 - filter is a fixed internal fragment
            (*history_parameters, result_limit, *history_parameters, ALPHA_HISTORY_POINTS),
        ).fetchall()

    teams: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in event_rows:
        event = dict(raw)
        for side, opponent in (("home", "away"), ("away", "home")):
            team_id = str(event[f"{side}_team_id"])
            key = (str(event["league"]), team_id)
            team = teams.setdefault(
                key,
                {
                    "league": str(event["league"]),
                    "team_id": team_id,
                    "name": str(event[f"{side}_team_name"]),
                    "abbreviation": str(event[f"{side}_abbreviation"]),
                    "record": None,
                    "outcomes": [],
                },
            )
            record = _record(event.get(f"{side}_record"))
            if record:
                team["record"] = record
            side_score = _number(event.get(f"{side}_score"))
            opponent_score = _number(event.get(f"{opponent}_score"))
            if event.get("completed") and side_score is not None and opponent_score is not None:
                if side_score != opponent_score:
                    team["outcomes"].append((str(event["start_time"]), side_score > opponent_score))

    team_rows: list[dict[str, Any]] = []
    for team in teams.values():
        record = team["record"]
        outcomes = team.pop("outcomes")
        if record:
            wins, losses = record
        else:
            wins = sum(won for _, won in outcomes)
            losses = len(outcomes) - wins
        games = wins + losses
        if not games:
            continue
        history_points = _rate_history_points(outcomes, (wins, losses))
        history = [point["rate"] for point in history_points]
        recent = outcomes[-10:]
        recent_rate = (
            round(sum(won for _, won in recent) / len(recent) * 100, 1)
            if recent
            else round(wins / games * 100, 1)
        )
        team_rows.append(
            {
                **team,
                "wins": wins,
                "losses": losses,
                "games": games,
                "stored_games": len(outcomes),
                "win_rate": round(wins / games * 100, 1),
                "recent_win_rate": recent_rate,
                "trend_pct": round(history[-1] - history[0], 1) if len(history) > 1 else 0.0,
                "history": history,
                "history_points": history_points,
                "history_start": history_points[0]["at"] if history_points else "",
                "history_end": history_points[-1]["at"] if history_points else "",
                "rank_score": (wins + 4) / (games + 8),
            }
        )
    team_rows.sort(key=lambda row: (-row["rank_score"], -row["games"], row["abbreviation"]))

    eligible_player_count = int(player_rows[0]["eligible_player_count"]) if player_rows else 0
    player_groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
    for raw in player_rows:
        row = dict(raw)
        key = (str(row["league"]), str(row["player_id"]))
        player = player_groups.setdefault(
            key,
            {
                "league": str(row["league"]),
                "player_id": str(row["player_id"]),
                "name": str(row["player_name"]),
                "team_name": str(row["team_name"]),
                "team_abbreviation": str(row["team_abbreviation"]),
                "position": str(row["position"] or "Player"),
                "outcomes": [],
            },
        )
        player["outcomes"].append((str(row["start_time"]), bool(row["won"])))
    player_output: list[dict[str, Any]] = []
    for player in player_groups.values():
        outcomes = player.pop("outcomes")
        games = len(outcomes)
        if games < 3:
            continue
        wins = sum(won for _, won in outcomes)
        history_points = _rate_history_points(outcomes)
        history = [point["rate"] for point in history_points]
        player_output.append(
            {
                **player,
                "wins": wins,
                "losses": games - wins,
                "games": games,
                "win_rate": round(wins / games * 100, 1),
                "trend_pct": round(history[-1] - history[0], 1) if len(history) > 1 else 0.0,
                "history": history,
                "history_points": history_points,
                "history_start": history_points[0]["at"] if history_points else "",
                "history_end": history_points[-1]["at"] if history_points else "",
                "rank_score": (wins + 2) / (games + 4),
            }
        )
    player_output.sort(key=lambda row: (-row["rank_score"], -row["games"], row["name"]))
    leagues_with_data = {str(team["league"]) for team in team_rows}
    coverage = dict(coverage_row) if coverage_row else {}
    return {
        "model": _model_alpha(selected_league),
        "teams": team_rows[:result_limit],
        "players": player_output[:result_limit],
        "player_min_games": 3,
        "league": selected_league,
        "leagues": [
            {"key": key, "name": value["name"], "has_data": key in leagues_with_data}
            for key, value in LEAGUES.items()
        ],
        "coverage": {
            "events": int(coverage.get("events") or 0),
            "completed_games": int(coverage.get("completed") or 0),
            "history_start": str(coverage.get("history_start") or ""),
            "history_end": str(coverage.get("history_end") or ""),
            "team_count": len(team_rows),
            "player_count": eligible_player_count,
        },
        "updated_at": str(coverage.get("updated_at") or ""),
    }


def _score_display(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "—"
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _history_game(row: Any, team_id: str | None = None) -> dict[str, Any]:
    item = dict(row)
    game = {
        "id": str(item["id"]),
        "start_time": str(item["start_time"]),
        "status_detail": str(item["status_detail"]),
        "home_team_id": str(item["home_team_id"]),
        "home_team_name": str(item["home_team_name"]),
        "home_abbreviation": str(item["home_abbreviation"]),
        "home_score": _score_display(item["home_score"]),
        "away_team_id": str(item["away_team_id"]),
        "away_team_name": str(item["away_team_name"]),
        "away_abbreviation": str(item["away_abbreviation"]),
        "away_score": _score_display(item["away_score"]),
    }
    if team_id is None:
        return game
    is_home = game["home_team_id"] == team_id
    team_score = _number(item["home_score"] if is_home else item["away_score"])
    opponent_score = _number(item["away_score"] if is_home else item["home_score"])
    if team_score is None or opponent_score is None:
        result = "—"
    elif team_score > opponent_score:
        result = "W"
    elif team_score < opponent_score:
        result = "L"
    else:
        result = "T"
    game.update(
        {
            "result": result,
            "venue_word": "vs" if is_home else "at",
            "opponent_abbreviation": (
                game["away_abbreviation"] if is_home else game["home_abbreviation"]
            ),
            "team_score": game["home_score"] if is_home else game["away_score"],
            "opponent_score": game["away_score"] if is_home else game["home_score"],
        }
    )
    return game


def _recent_team_form(
    database: Any,
    event: dict[str, Any],
    team_id: str,
    abbreviation: str,
) -> dict[str, Any]:
    rows = database.execute(
        """
        SELECT id,start_time,status_detail,home_team_id,home_team_name,home_abbreviation,
               home_score,away_team_id,away_team_name,away_abbreviation,away_score
        FROM sports_events
        WHERE league=? AND completed=1 AND start_time<?
          AND (home_team_id=? OR away_team_id=?)
        ORDER BY start_time DESC,id DESC LIMIT 5
        """,
        (event["league"], event["start_time"], team_id, team_id),
    ).fetchall()
    games = [_history_game(row, team_id) for row in rows]
    wins = sum(game["result"] == "W" for game in games)
    losses = sum(game["result"] == "L" for game in games)
    ties = sum(game["result"] == "T" for game in games)
    record = f"{wins}-{losses}" + (f"-{ties}" if ties else "")
    rest_label = "No prior game stored"
    short_rest = False
    if games:
        between_starts = _parse_time(str(event["start_time"])) - _parse_time(games[0]["start_time"])
        minutes = max(0, int(between_starts.total_seconds() // 60))
        hours, minute_part = divmod(minutes, 60)
        short_rest = timedelta(0) < between_starts <= BACK_TO_BACK_MAX_GAP
        if hours < 48:
            rest_label = f"{hours}h {minute_part}m since last start"
        else:
            days = hours // 24
            rest_label = f"{days}d since last start"
    return {
        "team_id": team_id,
        "abbreviation": abbreviation,
        "record": record,
        "games": games,
        "rest_label": rest_label,
        "short_rest": short_rest,
    }


def _series_context(database: Any, event: dict[str, Any]) -> dict[str, Any]:
    home_id = str(event["home_team_id"])
    away_id = str(event["away_team_id"])
    start = _parse_time(event["start_time"])
    history_columns = """
        id,start_time,status_detail,home_team_id,home_team_name,home_abbreviation,
        home_score,away_team_id,away_team_name,away_abbreviation,away_score
    """
    previous_rows = database.execute(
        f"""
        SELECT {history_columns}
        FROM sports_events
        WHERE league=? AND completed=1 AND start_time<?
          AND ((home_team_id=? AND away_team_id=?)
            OR (home_team_id=? AND away_team_id=?))
        ORDER BY start_time DESC,id DESC LIMIT 5
        """,  # noqa: S608 - selected columns are fixed above
        (event["league"], event["start_time"], home_id, away_id, away_id, home_id),
    ).fetchall()
    previous_meetings = [_history_game(row) for row in previous_rows]

    home_wins = 0
    away_wins = 0
    ties = 0
    for row in previous_rows:
        home_score = _number(row["home_score"])
        away_score = _number(row["away_score"])
        if home_score is None or away_score is None:
            continue
        if home_score == away_score:
            ties += 1
            continue
        winner_id = str(row["home_team_id"] if home_score > away_score else row["away_team_id"])
        if winner_id == home_id:
            home_wins += 1
        elif winner_id == away_id:
            away_wins += 1

    series_rows = database.execute(
        f"""
        SELECT {history_columns}
        FROM sports_events
        WHERE league=? AND start_time>=? AND start_time<=?
          AND ((home_team_id=? AND away_team_id=?)
            OR (home_team_id=? AND away_team_id=?))
        ORDER BY start_time,id
        """,  # noqa: S608 - selected columns are fixed above
        (
            event["league"],
            _iso(start - SERIES_WINDOW),
            _iso(start + SERIES_WINDOW),
            home_id,
            away_id,
            away_id,
            home_id,
        ),
    ).fetchall()
    series_games = [_history_game(row) for row in series_rows]
    current_index = next(
        (index for index, game in enumerate(series_games) if game["id"] == event["id"]),
        None,
    )
    if current_index is not None:
        left = current_index
        right = current_index
        while left > 0:
            gap = _parse_time(series_games[left]["start_time"]) - _parse_time(
                series_games[left - 1]["start_time"]
            )
            if gap > SERIES_MAX_GAP:
                break
            left -= 1
        while right + 1 < len(series_games):
            gap = _parse_time(series_games[right + 1]["start_time"]) - _parse_time(
                series_games[right]["start_time"]
            )
            if gap > SERIES_MAX_GAP:
                break
            right += 1
        series_games = series_games[left : right + 1]
        current_index -= left
    else:
        series_games = []

    previous = previous_meetings[0] if previous_meetings else None
    between_starts = start - _parse_time(previous["start_time"]) if previous else None
    back_to_back = bool(
        between_starts and timedelta(0) < between_starts <= BACK_TO_BACK_MAX_GAP
    )
    if previous:
        recap = database.execute(
            """
            SELECT * FROM sports_news_articles WHERE event_id=?
            ORDER BY published_at DESC,id DESC LIMIT 1
            """,
            (previous["id"],),
        ).fetchone()
        previous["recap"] = dict(recap) if recap else None

    if back_to_back:
        headline = "Back-to-back rematch"
    elif len(series_games) > 1 and current_index is not None:
        headline = f"Game {current_index + 1} of {len(series_games)} in this series"
    elif previous:
        headline = "Previous matchup and recent form"
    else:
        headline = "Recent team form"

    between_starts_label = None
    if between_starts:
        minutes = int(between_starts.total_seconds() // 60)
        hours, minute_part = divmod(minutes, 60)
        between_starts_label = f"{hours}h {minute_part}m between starts"

    return {
        "headline": headline,
        "back_to_back": back_to_back,
        "between_starts_label": between_starts_label,
        "series_game_number": current_index + 1 if current_index is not None else None,
        "series_game_count": len(series_games),
        "previous_meeting": previous,
        "head_to_head": {
            "meetings": len(previous_meetings),
            "home_wins": home_wins,
            "away_wins": away_wins,
            "ties": ties,
        },
        "recent_form": [
            _recent_team_form(database, event, away_id, str(event["away_abbreviation"])),
            _recent_team_form(database, event, home_id, str(event["home_abbreviation"])),
        ],
    }


def _player_stats(stats_json: Any) -> tuple[dict[str, Any], str]:
    try:
        raw = json.loads(str(stats_json or "{}"))
    except (TypeError, ValueError):
        raw = {}
    stats = raw if isinstance(raw, dict) else {}
    labels: list[str] = []
    for group in stats.values():
        if not isinstance(group, dict):
            continue
        for name, value in group.items():
            label = f"{name} {value}".strip()
            if label and label not in labels:
                labels.append(label)
            if len(labels) == 4:
                break
        if len(labels) == 4:
            break
    return stats, " · ".join(labels)


def _matchup_player_context(database: Any, event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only players tied to the latest stored roster for this matchup."""

    team_ids = [str(event["away_team_id"]), str(event["home_team_id"])]
    roster_events = database.execute(
        """
        SELECT team_id,event_id,start_time FROM (
            SELECT a.team_id,a.event_id,e.start_time,
                   ROW_NUMBER() OVER(
                       PARTITION BY a.team_id ORDER BY e.start_time DESC,a.event_id DESC
                   ) AS latest_rank
            FROM sports_player_appearances a
            JOIN sports_events e ON e.id=a.event_id
            WHERE a.league=? AND e.start_time<? AND a.team_id IN (?,?)
            GROUP BY a.team_id,a.event_id,e.start_time
        ) latest WHERE latest_rank=1
        """,
        (event["league"], event["start_time"], *team_ids),
    ).fetchall()
    roster_event_by_team = {
        str(row["team_id"]): str(row["event_id"]) for row in roster_events
    }
    if not roster_event_by_team:
        return []

    roster_event_ids = list(roster_event_by_team.values())
    placeholders = ",".join("?" for _ in roster_event_ids)
    roster_rows = database.execute(
        f"""
        SELECT * FROM sports_player_appearances
        WHERE event_id IN ({placeholders})
        ORDER BY starter DESC,player_name,player_id
        """,  # noqa: S608 - placeholders are generated above
        tuple(roster_event_ids),
    ).fetchall()
    roster = [dict(row) for row in roster_rows]
    if not roster:
        return []

    player_ids = list(dict.fromkeys(str(row["player_id"]) for row in roster))
    player_placeholders = ",".join("?" for _ in player_ids)
    cutoff = _iso(_parse_time(event["start_time"]) - timedelta(days=HISTORY_TARGET_DAYS))
    history_rows = database.execute(
        f"""
        SELECT a.player_id,a.team_id,a.won,e.start_time
        FROM sports_player_appearances a
        JOIN sports_events e ON e.id=a.event_id
        WHERE a.league=? AND e.start_time>=? AND e.start_time<?
          AND a.player_id IN ({player_placeholders})
        ORDER BY e.start_time,a.event_id
        """,  # noqa: S608 - placeholders are generated above
        (event["league"], cutoff, event["start_time"], *player_ids),
    ).fetchall()
    outcomes: dict[tuple[str, str], list[tuple[str, bool]]] = defaultdict(list)
    for row in history_rows:
        outcomes[(str(row["team_id"]), str(row["player_id"]))].append(
            (str(row["start_time"]), bool(row["won"]))
        )

    teams: dict[str, list[dict[str, Any]]] = {team_id: [] for team_id in team_ids}
    for player in roster:
        team_id = str(player["team_id"])
        player_outcomes = outcomes.get((team_id, str(player["player_id"])), [])
        games = len(player_outcomes)
        wins = sum(won for _, won in player_outcomes)
        stats, stats_label = _player_stats(player.get("stats_json"))
        teams.setdefault(team_id, []).append(
            {
                "player_id": str(player["player_id"]),
                "name": str(player["player_name"]),
                "position": str(player.get("position") or "Player"),
                "starter": bool(player.get("starter")),
                "games": games,
                "wins": wins,
                "losses": games - wins,
                "win_rate": round(wins / games * 100, 1) if games else None,
                "last_stats": stats,
                "last_stats_label": stats_label,
                "last_seen_at": next(
                    (
                        str(row["start_time"])
                        for row in roster_events
                        if str(row["team_id"]) == team_id
                    ),
                    "",
                ),
            }
        )

    output = []
    for side in ("away", "home"):
        team_id = str(event[f"{side}_team_id"])
        players = teams.get(team_id, [])
        players.sort(
            key=lambda item: (
                not bool(item["starter"]),
                -int(item["games"]),
                str(item["name"]),
            )
        )
        output.append(
            {
                "side": side,
                "team_id": team_id,
                "team_name": str(event[f"{side}_team_name"]),
                "team_abbreviation": str(event[f"{side}_abbreviation"]),
                "record": str(event.get(f"{side}_record") or "No season record"),
                "players": players[:8],
                "source_event_id": roster_event_by_team.get(team_id),
            }
        )
    return output


def sports_event(event_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute("SELECT * FROM sports_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return None
        event = _event_row(database, row)
        prediction = _pregame_prediction(database, event_id, str(event["start_time"]))
        prediction_cutoff = (
            str(prediction["observed_at"]) if prediction else str(event["start_time"])
        )
        receipt_odds = _odds_at_or_before(database, event_id, prediction_cutoff)
        event["prediction"] = prediction
        event["odds"] = receipt_odds
        event["edge_history"] = _edge_sparkline(database, event, prediction)
        event.update(_event_attention(event))
        event["receipt"] = _prediction_receipt(event, prediction, receipt_odds)
        odds_rows = database.execute(
            """
            SELECT * FROM sports_odds_snapshots WHERE event_id=? AND observed_at<=?
            ORDER BY observed_at DESC,id DESC LIMIT 30
            """,
            (event_id, event["start_time"]),
        ).fetchall()
        event["odds_history"] = [
            odds_item
            for item in reversed(odds_rows)
            if (odds_item := _odds_item(item)) is not None
        ]
        pick_rows = database.execute(
            """
            SELECT p.*,ci.handle AS caller_handle FROM sports_picks p
            JOIN caller_identities ci ON ci.id=p.caller_identity_id
            WHERE p.event_id=? ORDER BY p.created_at DESC LIMIT 50
            """,
            (event_id,),
        ).fetchall()
        event["picks"] = []
        for item in pick_rows:
            pick = dict(item)
            pick["reward_flash"] = (
                sports_call_reward(pick.get("american_odds")) if pick.get("result") == "win" else 0
            )
            event["picks"].append(pick)
        news_rows = database.execute(
            """
            SELECT * FROM sports_news_articles WHERE event_id=?
            ORDER BY published_at DESC,id DESC LIMIT 8
            """,
            (event_id,),
        ).fetchall()
        event["news"] = [dict(item) for item in news_rows]
        event["context"] = _series_context(database, event)
        event["matchup_players"] = _matchup_player_context(database, event)
    event["model_record"] = _model_alpha(str(event["league"]))
    return event


def sports_flash_evidence(event_id: str) -> tuple[str, dict[str, Any]]:
    """Build a source-bound Flash snapshot for one game, never a global player list."""

    event = sports_event(event_id)
    if not event:
        raise ValueError("Game not found")
    prediction = dict(event.get("prediction") or {})
    odds = dict(event.get("odds") or {})
    context = dict(event.get("context") or {})
    winner_side = str(event.get("model_winner_side") or "home")
    winner_probability = event.get("model_winner_probability_pct")

    pick_counts: dict[str, dict[str, int]] = {}
    for pick in event.get("picks") or []:
        selection = str(pick.get("selection") or "unknown")
        counts = pick_counts.setdefault(
            selection,
            {"total": 0, "open": 0, "wins": 0, "losses": 0, "pushes": 0},
        )
        counts["total"] += 1
        status = str(pick.get("status") or "")
        result = str(pick.get("result") or "")
        if status == "open":
            counts["open"] += 1
        elif result in {"win", "loss", "push"}:
            result_key = {"win": "wins", "loss": "losses", "push": "pushes"}[result]
            counts[result_key] += 1

    source_values = [str(event.get("source_url") or "")]
    source_values.extend(
        str(article.get("source_url") or "") for article in event.get("news") or []
    )
    previous = context.get("previous_meeting") or {}
    if isinstance(previous, dict) and isinstance(previous.get("recap"), dict):
        source_values.append(str(previous["recap"].get("source_url") or ""))

    evidence = {
        "subject_type": "sports_game",
        "event_id": str(event["id"]),
        "league": str(event["league"]),
        "matchup": f"{event['away_team_name']} at {event['home_team_name']}",
        "start_time": str(event["start_time"]),
        "status": str(event["status"]),
        "captured_at": _iso(),
        "winner": {
            "side": winner_side,
            "team_id": str(event.get("model_winner_team_id") or ""),
            "team_name": str(event.get("model_winner_team_name") or "Unknown"),
            "abbreviation": str(event.get("model_winner_abbreviation") or "GAME"),
            "model_probability_pct": winner_probability,
        },
        "prediction": {
            "model_version": prediction.get("model_version"),
            "selection": prediction.get("selection"),
            "signal": prediction.get("signal"),
            "quality": prediction.get("quality"),
            "home_probability": prediction.get("home_probability"),
            "away_probability": prediction.get("away_probability"),
            "home_market_probability": prediction.get("home_market_probability"),
            "away_market_probability": prediction.get("away_market_probability"),
            "edge_pct": prediction.get("edge_pct"),
            "evidence": prediction.get("evidence") or [],
            "risks": prediction.get("risks") or [],
            "observed_at": prediction.get("observed_at"),
            "input_hash": prediction.get("input_hash"),
        },
        "odds": {
            "sportsbook": odds.get("sportsbook"),
            "observed_at": odds.get("observed_at"),
            "home": odds.get("home_odds"),
            "away": odds.get("away_odds"),
            "home_open": odds.get("home_open_odds"),
            "away_open": odds.get("away_open_odds"),
            "total": odds.get("total"),
            "history": event.get("odds_history") or [],
        },
        "market_comparison": event.get("market_comparison") or {},
        "teams": {
            side: {
                "id": str(event[f"{side}_team_id"]),
                "name": str(event[f"{side}_team_name"]),
                "abbreviation": str(event[f"{side}_abbreviation"]),
                "season_record": str(event.get(f"{side}_record") or "unknown"),
            }
            for side in ("away", "home")
        },
        "series_and_form": context,
        "players": event.get("matchup_players") or [],
        "news": [
            {
                "team_side": article.get("team_side"),
                "headline": article.get("headline"),
                "summary": article.get("summary"),
                "source_name": article.get("source_name"),
                "source_url": article.get("source_url"),
                "published_at": article.get("published_at"),
            }
            for article in event.get("news") or []
        ],
        "public_picks": pick_counts,
        "forecast_contract": {
            "version": SPORTS_FORECAST_CONTRACT_VERSION,
            "subject": "pregame moneyline winner",
            "selections": ["home", "away", "pass"],
            "probabilities": "home and away must sum to 1",
            "scoring": "Brier score after the final result",
            "independence": (
                "Make an AI forecast from the supplied evidence. Keep it separate from the "
                "team-form baseline and the market price."
            ),
        },
        "sources": list(dict.fromkeys(value for value in source_values if value)),
    }
    fingerprint = hashlib.sha256(
        _json(
            {
                "event_id": event["id"],
                "prediction": prediction.get("input_hash"),
                "odds": odds.get("snapshot_hash") or odds.get("observed_at"),
                "players": [
                    team.get("source_event_id") for team in event.get("matchup_players") or []
                ],
                "news": [article.get("id") for article in event.get("news") or []],
                "picks": [pick.get("updated_at") for pick in event.get("picks") or []],
            }
        ).encode()
    ).hexdigest()
    evidence["evidence_fingerprint"] = fingerprint
    return fingerprint, evidence


def record_sports_ai_forecast(
    database: Any,
    *,
    report_id: str,
    evidence: dict[str, Any],
    forecast: dict[str, Any],
    actor: dict[str, Any],
    resolved_model: str,
    observed_at: str,
) -> dict[str, Any]:
    """Store one immutable model forecast before the game starts."""

    try:
        predicted_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        starts_at = datetime.fromisoformat(
            str(evidence["start_time"]).replace("Z", "+00:00")
        )
        if predicted_at.tzinfo is None:
            predicted_at = predicted_at.replace(tzinfo=UTC)
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Sports AI forecast timing is invalid") from exc
    if predicted_at >= starts_at:
        raise ValueError("Sports AI forecasts must be completed before the game starts")

    forecast_id = str(uuid.uuid4())
    database.execute(
        """
        INSERT INTO sports_ai_forecasts(
            id,report_id,event_id,league,actor_id,actor_snapshot_json,provider,
            requested_model,resolved_model,ladder_position,ladder_size,
            evidence_fingerprint,selection,home_probability,away_probability,
            confidence,reason,contract_version,observed_at,start_time,status
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')
        ON CONFLICT(report_id) DO NOTHING
        """,
        (
            forecast_id,
            report_id,
            str(evidence["event_id"]),
            str(evidence["league"]),
            str(actor.get("id") or "unknown"),
            _json(actor),
            str(actor.get("provider") or "unknown"),
            str(actor.get("model") or resolved_model),
            resolved_model,
            int(actor.get("ladder_position") or 1),
            int(actor.get("ladder_size") or KOL_LADDER_SIZE),
            str(evidence.get("evidence_fingerprint") or ""),
            forecast["selection"],
            forecast["home_probability"],
            forecast["away_probability"],
            forecast["confidence"],
            forecast["reason"],
            forecast["contract_version"],
            observed_at,
            str(evidence["start_time"]),
        ),
    )
    row = database.execute(
        "SELECT * FROM sports_ai_forecasts WHERE report_id=?",
        (report_id,),
    ).fetchone()
    if not row:
        raise RuntimeError("Sports AI forecast was not stored")
    return dict(row)


def sports_ai_tournament(league: str = "all") -> dict[str, Any]:
    """Return four durable model slots and scored pregame forecast records."""

    selected_league = league if league in LEAGUES else "all"
    league_filter = " WHERE league=?" if selected_league in LEAGUES else ""
    parameters: tuple[Any, ...] = (selected_league,) if league_filter else ()
    with connection() as database:
        rows = database.execute(
            f"""
            SELECT * FROM (
                SELECT f.*,ROW_NUMBER() OVER(
                    PARTITION BY actor_id,resolved_model,event_id
                    ORDER BY observed_at DESC,id DESC
                ) AS final_rank
                FROM sports_ai_forecasts f{league_filter}
            ) ranked
            WHERE final_rank=1
            ORDER BY observed_at,id
            """,  # noqa: S608 - filter is a fixed internal fragment
            parameters,
        ).fetchall()

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for raw in rows:
        item = dict(raw)
        grouped[(str(item["actor_id"]), str(item["resolved_model"]))].append(item)

    cards: list[dict[str, Any]] = []
    resolved_events: dict[tuple[str, str], set[str]] = {}
    for identity, forecasts in grouped.items():
        actor_id, resolved_model = identity
        try:
            actor = json.loads(str(forecasts[-1]["actor_snapshot_json"] or "{}"))
        except (TypeError, ValueError):
            actor = {}
        if not isinstance(actor, dict):
            actor = {}
        settled = [item for item in forecasts if item.get("brier_score") is not None]
        decisions = [item for item in settled if item.get("result") in {"win", "loss"}]
        hits = sum(item.get("result") == "win" for item in decisions)
        resolved_events[identity] = {str(item["event_id"]) for item in settled}
        cards.append(
            {
                "actor_id": actor_id,
                "display_name": str(actor.get("display_name") or actor_id),
                "emoji": str(actor.get("emoji") or "AI"),
                "model": resolved_model,
                "model_label": model_display_name(resolved_model),
                "position": int(actor.get("ladder_position") or forecasts[-1]["ladder_position"]),
                "forecasts": len(forecasts),
                "open": sum(item["status"] == "open" for item in forecasts),
                "scored": len(settled),
                "hits": hits,
                "accuracy": round(hits / len(decisions) * 100, 1) if decisions else None,
                "brier": (
                    round(
                        sum(float(item["brier_score"]) for item in settled) / len(settled),
                        3,
                    )
                    if settled
                    else None
                ),
                "last_forecast_at": str(forecasts[-1]["observed_at"]),
            }
        )

    latest_by_position: dict[int, dict[str, Any]] = {}
    for card in cards:
        position = int(card["position"])
        current = latest_by_position.get(position)
        if not current or str(card["last_forecast_at"]) > str(current["last_forecast_at"]):
            latest_by_position[position] = card
    current_actor = actor_snapshot(FLASH)
    current_identity = (str(current_actor["id"]), str(current_actor["model"]))
    current_card = next(
        (
            card
            for card in cards
            if (str(card["actor_id"]), str(card["model"])) == current_identity
        ),
        None,
    )
    latest_by_position[int(current_actor["ladder_position"])] = current_card or {
        "actor_id": current_actor["id"],
        "display_name": current_actor["display_name"],
        "emoji": current_actor["emoji"],
        "model": current_actor["model"],
        "model_label": current_actor["model_label"],
        "position": current_actor["ladder_position"],
        "forecasts": 0,
        "open": 0,
        "scored": 0,
        "hits": 0,
        "accuracy": None,
        "brier": None,
        "last_forecast_at": "",
    }

    active_identities = [
        (str(card["actor_id"]), str(card["model"])) for card in latest_by_position.values()
    ]
    shared_events: set[str] = set()
    if len(active_identities) > 1:
        event_sets = [resolved_events.get(identity, set()) for identity in active_identities]
        shared_events = set.intersection(*event_sets) if event_sets else set()

    slots: list[dict[str, Any]] = []
    for position in range(1, KOL_LADDER_SIZE + 1):
        card = latest_by_position.get(position)
        if card:
            slots.append(
                {
                    **card,
                    "status": "champion" if position == 1 else "challenger",
                }
            )
        else:
            slots.append(
                {
                    "position": position,
                    "status": "open",
                    "display_name": "Future model",
                    "emoji": "+",
                    "model": None,
                    "model_label": "Open challenger slot",
                    "forecasts": 0,
                    "open": 0,
                    "scored": 0,
                    "hits": 0,
                    "accuracy": None,
                    "brier": None,
                }
            )
    return {
        "league": selected_league,
        "ladder_size": KOL_LADDER_SIZE,
        "slots": slots,
        "shared_scored_games": len(shared_events),
        "comparison_ready": len(shared_events) >= 20,
        "contract_version": SPORTS_FORECAST_CONTRACT_VERSION,
    }


def create_sports_pick(
    user_id: str,
    event_id: str,
    selection: str,
) -> dict[str, Any]:
    if selection not in {"home", "away"}:
        raise ValueError("Pick must be home or away")
    timestamp = _iso()
    with connection() as database:
        existing = database.execute(
            """
            SELECT * FROM sports_picks
            WHERE user_id=? AND event_id=? AND market='moneyline'
            """,
            (user_id, event_id),
        ).fetchone()
        if existing:
            return dict(existing)
        event = database.execute(
            "SELECT * FROM sports_events WHERE id=? AND status='pre' AND start_time>?",
            (event_id, timestamp),
        ).fetchone()
        if not event:
            raise ValueError("This game is no longer open for picks")
        bookmaker_rows = _latest_bookmaker_rows(database, [event_id])
        comparison = _market_comparison(dict(event), bookmaker_rows)
        odds = _paper_moneyline(
            dict(event),
            _latest_odds(database, event_id),
            comparison,
            has_bookmaker_rows=bool(bookmaker_rows),
        )
        if not odds:
            raise ValueError("A fresh moneyline is not available")
        american_odds = odds[f"{selection}_odds"]
        if american_odds is None:
            raise ValueError("Moneyline odds are not available for that side")
        identity = ensure_caller_identity_with_database(database, user_id)
        prediction = _latest_prediction(database, event_id)
        pick_id = str(uuid.uuid4())
        public_id = secrets.token_urlsafe(9).replace("-", "").replace("_", "")[:12]
        database.execute(
            """
            INSERT INTO sports_picks(
                id,public_id,user_id,caller_identity_id,event_id,market,selection,
                line,american_odds,sportsbook,odds_observed_at,prediction_id,status,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,'moneyline',?,?,?, ?,?,?, 'open',?,?)
            """,
            (
                pick_id,
                public_id,
                user_id,
                identity["id"],
                event_id,
                selection,
                None,
                int(american_odds),
                odds.get("sportsbook") or "Unknown",
                odds["observed_at"],
                prediction.get("id") if prediction else None,
                timestamp,
                timestamp,
            ),
        )
        row = database.execute(
            """
            SELECT p.*,ci.handle AS caller_handle FROM sports_picks p
            JOIN caller_identities ci ON ci.id=p.caller_identity_id WHERE p.id=?
            """,
            (pick_id,),
        ).fetchone()
    return dict(row)


def sports_pick_stats() -> dict[str, Any]:
    with connection() as database:
        rows = database.execute(
            "SELECT result,return_units FROM sports_picks WHERE status='settled'"
        ).fetchall()
        open_count = database.execute(
            "SELECT COUNT(*) AS count FROM sports_picks WHERE status='open'"
        ).fetchone()
    settled = len(rows)
    units = round(sum(float(row["return_units"] or 0) for row in rows), 2)
    return {
        "settled": settled,
        "open": int(open_count["count"]) if open_count else 0,
        "wins": sum(row["result"] == "win" for row in rows),
        "losses": sum(row["result"] == "loss" for row in rows),
        "pushes": sum(row["result"] == "push" for row in rows),
        "units": units,
        "roi_pct": round(units / settled * 100, 1) if settled else None,
    }


def _odds_label(value: Any) -> str:
    odds = _american(value)
    return f"{odds:+d}" if odds is not None else "—"


def _selection_market_probability(
    selection: str,
    home_odds: Any,
    away_odds: Any,
) -> float | None:
    home_probability, away_probability = no_vig_probabilities(home_odds, away_odds)
    return home_probability if selection == "home" else away_probability


def _sports_alpha_item(
    event: dict[str, Any],
    selection: str,
    *,
    active_calls: int = 0,
    total_calls: int = 0,
) -> dict[str, Any]:
    event_id = str(event.get("event_id") or event["id"])
    abbreviation = str(event.get(f"{selection}_abbreviation") or "—")
    team_name = str(event.get(f"{selection}_team_name") or abbreviation)
    current_odds = event.get(f"current_{selection}_odds")
    current_probability = _selection_market_probability(
        selection,
        event.get("current_home_odds"),
        event.get("current_away_odds"),
    )
    opening_probability = _selection_market_probability(
        selection,
        event.get("opening_home_odds"),
        event.get("opening_away_odds"),
    )
    probability_move = (
        round((current_probability - opening_probability) * 100, 1)
        if current_probability is not None and opening_probability is not None
        else None
    )
    seed = f"{event.get(f'{selection}_team_id')}:{abbreviation}".encode()
    return {
        "ticker": abbreviation,
        "company": f"{team_name} to win",
        "coin_label": abbreviation[:3],
        "coin_tone": int(hashlib.sha256(seed).hexdigest()[:2], 16) % 5,
        "pulse_label": (
            f"{str(event.get('league') or '').upper()} · "
            f"{event.get('away_abbreviation')} at {event.get('home_abbreviation')}"
        ),
        "href": f"/game/{event_id}",
        "event_id": event_id,
        "selection": selection,
        "price_label": (
            f"{current_probability * 100:.1f}¢"
            if current_probability is not None
            else _odds_label(current_odds)
        ),
        "odds_label": _odds_label(current_odds),
        "change_label": (
            f"{probability_move:+.1f}pp" if probability_move is not None else "—"
        ),
        "change_tone": (
            "up" if probability_move is not None and probability_move >= 0 else "down"
        ),
        "active_calls": active_calls,
        "total_calls": total_calls,
    }


def sports_alpha_board(league: str = "all", limit: int = 50) -> dict[str, Any]:
    """Build public paper-Call activity for the Sports Alpha screen."""

    selected_league = league if league in LEAGUES else "all"
    parameters: list[Any] = []
    league_filter = ""
    if selected_league in LEAGUES:
        league_filter = " AND e.league=?"
        parameters.append(selected_league)
    parameters.append(max(1, min(limit * 10, 500)))
    with connection() as database:
        rows = database.execute(
            f"""
            WITH odds_ranked AS (
                SELECT o.*,
                       ROW_NUMBER() OVER(
                           PARTITION BY o.event_id ORDER BY o.observed_at DESC,o.id DESC
                       ) AS latest_rank,
                       ROW_NUMBER() OVER(
                           PARTITION BY o.event_id ORDER BY o.observed_at,o.id
                       ) AS opening_rank
                FROM sports_odds_snapshots o
            )
            SELECT p.*,ci.handle AS caller_handle,
                   COUNT(*) OVER() AS board_total_calls,
                   SUM(CASE WHEN p.status='open' THEN 1 ELSE 0 END)
                       OVER() AS board_active_calls,
                   COUNT(*) OVER(PARTITION BY p.event_id,p.selection)
                       AS group_total_calls,
                   SUM(CASE WHEN p.status='open' THEN 1 ELSE 0 END)
                       OVER(PARTITION BY p.event_id,p.selection)
                       AS group_active_calls,
                   e.league,e.start_time,e.status AS event_status,
                   e.home_team_id,e.home_team_name,e.home_abbreviation,
                   e.away_team_id,e.away_team_name,e.away_abbreviation,
                   latest.home_odds AS current_home_odds,
                   latest.away_odds AS current_away_odds,
                   opening.home_odds AS opening_home_odds,
                   opening.away_odds AS opening_away_odds
            FROM sports_picks p
            JOIN caller_identities ci ON ci.id=p.caller_identity_id
            JOIN sports_events e ON e.id=p.event_id
            LEFT JOIN odds_ranked latest
              ON latest.event_id=e.id AND latest.latest_rank=1
            LEFT JOIN odds_ranked opening
              ON opening.event_id=e.id AND opening.opening_rank=1
            WHERE 1=1{league_filter}
            ORDER BY p.updated_at DESC,p.id DESC LIMIT ?
            """,  # noqa: S608 - filter is a fixed internal fragment
            tuple(parameters),
        ).fetchall()

    picks = [dict(row) for row in rows]
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    calls: list[dict[str, Any]] = []
    for pick in picks:
        selection = str(pick["selection"])
        key = (str(pick["event_id"]), selection)
        group = grouped.setdefault(
            key,
            {
                "event": pick,
                "active_calls": int(pick["group_active_calls"]),
                "total_calls": int(pick["group_total_calls"]),
                "latest_activity": str(pick["updated_at"]),
            },
        )
        group["latest_activity"] = max(
            str(group["latest_activity"]), str(pick["updated_at"])
        )

        selected_name = str(pick[f"{selection}_team_name"])
        selected_abbreviation = str(pick[f"{selection}_abbreviation"])
        current_odds = pick.get(f"current_{selection}_odds")
        result = str(pick.get("result") or "")
        return_units = _number(pick.get("return_units"))
        reward = sports_call_reward(pick.get("american_odds")) if result == "win" else 0
        calls.append(
            {
                "caller_handle": str(pick["caller_handle"]),
                "caller_href": f"/u/{pick['caller_handle']}",
                "ticker": selected_abbreviation,
                "company": selected_name,
                "href": f"/game/{pick['event_id']}",
                "status": result if pick["status"] == "settled" and result else str(pick["status"]),
                "entry_label": _odds_label(pick.get("american_odds")),
                "mark_label": _odds_label(current_odds),
                "return_label": (
                    f"{return_units:+.2f}u" if return_units is not None else None
                ),
                "return_tone": (
                    "up" if return_units is not None and return_units >= 0 else "down"
                ),
                "reward_label": f"+{reward} Flash" if reward else None,
                "created_at": str(pick["created_at"]),
            }
        )

    ranked_groups = sorted(
        grouped.values(),
        key=lambda item: (
            int(item["active_calls"]),
            int(item["total_calls"]),
            str(item["latest_activity"]),
        ),
        reverse=True,
    )[: max(1, min(limit, 100))]
    board_rows = []
    for rank, group in enumerate(ranked_groups, start=1):
        item = _sports_alpha_item(
            group["event"],
            str(group["event"]["selection"]),
            active_calls=int(group["active_calls"]),
            total_calls=int(group["total_calls"]),
        )
        item.update(rank=rank, latest_activity=str(group["latest_activity"]))
        board_rows.append(item)

    ranked_keys = {(item["event_id"], item["selection"]) for item in board_rows}
    contenders = []
    for event in sports_pulse(selected_league, limit=8)["events"]:
        selection = str(event.get("model_winner_side") or "")
        if selection not in {"home", "away"}:
            continue
        key = (str(event["id"]), selection)
        if key in ranked_keys:
            continue
        odds = event.get("odds") or {}
        contender_event = {
            **event,
            "current_home_odds": odds.get("home_odds"),
            "current_away_odds": odds.get("away_odds"),
            "opening_home_odds": odds.get("home_open_odds"),
            "opening_away_odds": odds.get("away_open_odds"),
        }
        contenders.append(_sports_alpha_item(contender_event, selection))
        if len(contenders) == 5:
            break

    return {
        "rows": board_rows,
        "calls": calls[:100],
        "contenders": contenders,
        "total_calls": int(picks[0]["board_total_calls"]) if picks else 0,
        "active_calls": int(picks[0]["board_active_calls"]) if picks else 0,
        "league": selected_league,
        "leagues": [
            {"key": key, "name": value["name"]} for key, value in LEAGUES.items()
        ],
    }
