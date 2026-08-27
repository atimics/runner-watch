from __future__ import annotations

import hashlib
import json
import math
import secrets
import urllib.request
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from runner_watch.ingestion import SourceFetch
from runner_web.caller_ids import ensure_caller_identity_with_database
from runner_web.db import connection
from runner_web.ingestion import record_source_fetch

MODEL_VERSION = "team-form-v1"
SOURCE = "espn"
FEED = "sports_scoreboard_preview"
PLAYER_FEED = "sports_boxscore_preview"
NEWS_FEED = "sports_news_preview"
SOURCE_URL = "https://site.api.espn.com/apis/site/v2/sports"
PROMOTED_SIGNALS = {"lean", "watch"}
NEWS_MAX_AGE = timedelta(days=7)
NEWS_PER_EVENT = 6
LEAGUES = {
    "mlb": {"sport": "baseball", "path": "mlb", "name": "MLB", "home_edge": 0.035},
    "nfl": {"sport": "football", "path": "nfl", "name": "NFL", "home_edge": 0.055},
    "nba": {"sport": "basketball", "path": "nba", "name": "NBA", "home_edge": 0.060},
    "nhl": {"sport": "hockey", "path": "nhl", "name": "NHL", "home_edge": 0.040},
}


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
            f"https://www.espn.com/{LEAGUES[league]['sport']}/game/_/gameId/"
            f"{event.get('id')}"
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
        "unknown",
    }
    if not eligible_season:
        risks.append("Exhibition and preseason records are not used to promote an edge.")
    if home_record and away_record:
        home_rate = (home_record[0] + 8) / (sum(home_record) + 16)
        away_rate = (away_record[0] + 8) / (sum(away_record) + 16)
        probability = 0.5 + (home_rate - away_rate) * 0.65
        evidence.append(f"Season form: {away['record']} away, {home['record']} home.")
        quality = "baseline"
    else:
        probability = 0.5
        risks.append("One or both season records are missing.")
        quality = "thin"
    probability += float(LEAGUES[event["league"]]["home_edge"])
    home_probability = max(0.18, min(0.82, probability))
    away_probability = 1 - home_probability
    home_market, away_market = no_vig_probabilities(event["home_odds"], event["away_odds"])
    if home_market is not None and away_market is not None:
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


def _scoreboard_url(league: str, start: date, end: date) -> str:
    config = LEAGUES[league]
    date_range = f"{start:%Y%m%d}-{end:%Y%m%d}"
    return (
        f"{SOURCE_URL}/{config['sport']}/{config['path']}/scoreboard"
        f"?dates={date_range}&limit=100"
    )


def fetch_league(league: str, at: datetime | None = None) -> list[dict[str, Any]]:
    if league not in LEAGUES:
        raise ValueError("Unsupported league")
    current = at or datetime.now(UTC)
    locator = _scoreboard_url(
        league,
        current.date() - timedelta(days=1),
        current.date() + timedelta(days=3),
    )
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
                feed=FEED,
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
                feed=FEED,
                locator=locator,
                started_at=started,
                error=exc,
                metadata={"league": league},
            )
        )
        raise


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


def normalize_news_articles(
    events: list[dict[str, Any]],
    payload: dict[str, Any],
    collected_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """Match recent league headlines to Lean and Watch games by source team ID."""

    current = collected_at or datetime.now(UTC)
    promoted = [
        event for event in events if predict_event(event).get("signal") in PROMOTED_SIGNALS
    ]
    if not promoted:
        return []
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
        if not source_team_ids:
            continue
        external_id = str(raw.get("id") or "").strip()
        if not external_id:
            external_id = hashlib.sha256(source_url.encode()).hexdigest()[:32]
        summary = " ".join(str(raw.get("description") or "").split())[:500]
        source_name = str(raw.get("source") or "ESPN").strip()[:120] or "ESPN"
        for event in promoted:
            event_id = str(event["id"])
            if event_counts.get(event_id, 0) >= NEWS_PER_EVENT:
                continue
            home_match = str(event["home"]["id"]) in source_team_ids
            away_match = str(event["away"]["id"]) in source_team_ids
            if not home_match and not away_match:
                continue
            unique_key = (event_id, external_id)
            if unique_key in seen:
                continue
            seen.add(unique_key)
            event_counts[event_id] = event_counts.get(event_id, 0) + 1
            team_side = "both" if home_match and away_match else "home" if home_match else "away"
            article_id = hashlib.sha256(
                f"{SOURCE}|{event_id}|{external_id}".encode()
            ).hexdigest()[:32]
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


def store_news_articles(articles: list[dict[str, Any]]) -> int:
    with connection() as database:
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
                    article["id"], article["event_id"], article["provider"],
                    article["external_id"], article["team_side"], article["source_name"],
                    article["headline"], article["summary"], article["source_url"],
                    article["published_at"], article["collected_at"],
                ),
            )
    return len(articles)


def _summary_url(event: dict[str, Any]) -> str:
    league = str(event["league"])
    config = LEAGUES[league]
    return (
        f"{SOURCE_URL}/{config['sport']}/{config['path']}/summary"
        f"?event={event['external_id']}"
    )


def normalize_player_appearances(
    event: dict[str, Any], payload: dict[str, Any]
) -> list[dict[str, Any]]:
    """Normalize one completed box score into one row per player appearance."""

    home_score = _number(event["home"].get("score"))
    away_score = _number(event["away"].get("score"))
    if home_score is None or away_score is None or home_score == away_score:
        return []
    winning_team_id = str(
        event["home"]["id"] if home_score > away_score else event["away"]["id"]
    )
    team_lookup = {
        str(event[side]["id"]): event[side]
        for side in ("home", "away")
    }
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
                    str(uuid.uuid4()), row["event_id"], row["league"], row["team_id"],
                    row["team_name"], row["team_abbreviation"], row["player_id"],
                    row["player_name"], row["position"], int(row["starter"]),
                    int(row["won"]), _json(row["stats"]), timestamp,
                ),
            )
    return len(appearances)


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
            players += store_player_appearances(fetch_player_appearances(event))
        except Exception:
            errors += 1
    return {"events": fetched, "players": players, "errors": errors}


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
                    event["id"], SOURCE, event["external_id"], event["league"],
                    event["season_type"], event["name"],
                    _iso(event["start_time"]), event["status"], event["status_detail"],
                    int(event["completed"]), event["home"]["id"], event["home"]["name"],
                    event["home"]["abbreviation"], event["home"]["record"],
                    event["home"]["score"], event["away"]["id"], event["away"]["name"],
                    event["away"]["abbreviation"], event["away"]["record"],
                    event["away"]["score"], event["venue"], event["location"],
                    event["source_url"], timestamp, timestamp,
                ),
            )
            if event.get("home_odds") is not None or event.get("away_odds") is not None:
                odds_hash = hashlib.sha256(
                    _json(
                        {
                            "sportsbook": event["sportsbook"],
                            "home": event["home_odds"],
                            "away": event["away_odds"],
                            "home_open": event["home_open_odds"],
                            "away_open": event["away_open_odds"],
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
                        str(uuid.uuid4()), event["id"], SOURCE, event["sportsbook"],
                        "moneyline", event["home_odds"], event["away_odds"],
                        event["home_open_odds"], event["away_open_odds"], event["spread"],
                        event["total"], odds_hash, timestamp,
                    ),
                )
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
                    str(uuid.uuid4()), event["id"], prediction["model_version"], input_hash,
                    prediction["selection"], prediction["home_probability"],
                    prediction["away_probability"], prediction["home_market_probability"],
                    prediction["away_market_probability"], prediction["edge"],
                    prediction["signal"], prediction["quality"],
                    _json(prediction["evidence"]), _json(prediction["risks"]), timestamp,
                ),
            )
    return len(events)


def settle_picks() -> int:
    timestamp = _iso()
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
            settled += max(changed.rowcount, 0)
    return settled


def refresh_sports(at: datetime | None = None) -> dict[str, Any]:
    counts: dict[str, int] = {}
    player_counts: dict[str, dict[str, int]] = {}
    news_counts: dict[str, int] = {}
    news_errors: dict[str, str] = {}
    errors: dict[str, str] = {}
    for league in LEAGUES:
        try:
            events = fetch_league(league, at)
            counts[league] = store_events(events)
            player_counts[league] = collect_player_appearances(events)
        except Exception as exc:
            errors[league] = str(exc)[:240]
            continue
        if any(predict_event(event).get("signal") in PROMOTED_SIGNALS for event in events):
            try:
                news_counts[league] = store_news_articles(fetch_league_news(league, events, at))
            except Exception as exc:
                news_errors[league] = str(exc)[:240]
    settled = settle_picks()
    return {
        "counts": counts,
        "player_counts": player_counts,
        "news_counts": news_counts,
        "news_errors": news_errors,
        "errors": errors,
        "settled": settled,
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
    return dict(row) if row else None


def _latest_prediction(database: Any, event_id: str) -> dict[str, Any] | None:
    row = database.execute(
        """
        SELECT * FROM sports_predictions WHERE event_id=?
        ORDER BY observed_at DESC,id DESC LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
    item["risks"] = json.loads(item.pop("risks_json") or "[]")
    item["edge_pct"] = round(float(item["edge"]) * 100, 1) if item.get("edge") is not None else None
    return item


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

    if not prediction or not (side := _preferred_side(prediction)):
        return None
    rows = database.execute(
        """
        SELECT home_probability,away_probability,
               home_market_probability,away_market_probability,observed_at
        FROM sports_predictions
        WHERE event_id=? AND model_version=?
        ORDER BY observed_at DESC,id DESC LIMIT 24
        """,
        (event["id"], prediction["model_version"]),
    ).fetchall()
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
    for index, point in enumerate(points):
        x = chart_width / 2 if len(points) == 1 else 2 + index * 88 / (len(points) - 1)
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
        "dot_y": coordinates[0].split(",", 1)[1],
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
    abbreviation = str(event.get(f"{side}_abbreviation") or "—")
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
        signal_rank * 1000
        + abs(float(edge_pct or 0)) * 10
        + abs(float(market_move_pct or 0))
    )
    return {
        "signal_side": side,
        "signal_team_name": str(event.get(f"{side}_team_name") or "Unknown"),
        "signal_abbreviation": abbreviation,
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
    }


def _event_row(database: Any, row: Any) -> dict[str, Any]:
    item = dict(row)
    item["odds"] = _latest_odds(database, str(item["id"]))
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
        events = [_event_row(database, row) for row in rows]
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


def sports_pulse(
    league: str = "all", view: str = "signals", limit: int = 30
) -> dict[str, Any]:
    """Return promoted pre-game signals first, with the full slate kept one tap away."""

    payload = sports_slate(league, limit=200)
    available = [
        event for event in payload["events"] if str(event.get("status")) in {"pre", "in"}
    ]
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
    selected_view = "all" if view == "all" else "signals"
    if selected_view == "all":
        visible = sorted(
            available,
            key=lambda event: (
                0 if event.get("status") == "in" else 1,
                str(event.get("start_time") or ""),
                str(event.get("id") or ""),
            ),
        )
    else:
        visible = signals
    return {
        **payload,
        "events": visible[: max(1, min(limit, 100))],
        "view": selected_view,
        "signal_count": len(signals),
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
                radar_kind="model",
                radar_label="MODEL",
                radar_value=round(model_move, 1),
                radar_detail=(
                    f"{event['signal_abbreviation']} model edge {direction} by "
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
    return {
        **payload,
        "events": changes[: max(1, min(limit, 100))],
        "change_count": len(changes),
        "tracked_count": sum(
            bool(event.get("was_promoted"))
            for event in payload["events"]
            if event.get("status") in {"pre", "in"}
        ),
    }


def _rate_history(
    outcomes: list[tuple[str, bool]], current_record: tuple[int, int] | None = None
) -> list[float]:
    if current_record is None:
        wins = sum(won for _, won in outcomes)
        losses = len(outcomes) - wins
        running_wins = running_losses = 0
        points: list[float] = []
        for _, won in outcomes[-20:]:
            running_wins += int(won)
            running_losses += int(not won)
            points.append(round(running_wins / (running_wins + running_losses) * 100, 1))
        return points or ([round(wins / (wins + losses) * 100, 1)] if wins + losses else [])
    wins, losses = current_record
    recent = outcomes[-20:]
    previous_wins, previous_losses = wins, losses
    for _, won in reversed(recent):
        if won and previous_wins > 0:
            previous_wins -= 1
        elif not won and previous_losses > 0:
            previous_losses -= 1
    points = []
    if previous_wins + previous_losses:
        points.append(round(previous_wins / (previous_wins + previous_losses) * 100, 1))
    for _, won in recent:
        previous_wins += int(won)
        previous_losses += int(not won)
        points.append(round(previous_wins / (previous_wins + previous_losses) * 100, 1))
    return points or ([round(wins / (wins + losses) * 100, 1)] if wins + losses else [])


def sports_alpha(league: str = "all", limit: int = 24) -> dict[str, Any]:
    """Rank team and player results and keep the underlying rate history visible."""

    selected_league = league if league in LEAGUES else "all"
    parameters: tuple[Any, ...] = (selected_league,) if selected_league in LEAGUES else ()
    league_filter = " AND league=?" if parameters else ""
    player_league_filter = " AND a.league=?" if parameters else ""
    with connection() as database:
        event_rows = database.execute(
            f"""
            SELECT * FROM sports_events
            WHERE season_type NOT IN ('preseason','pre-season'){league_filter}
            ORDER BY start_time,id
            """,  # noqa: S608 - filter is a fixed internal fragment
            parameters,
        ).fetchall()
        player_rows = database.execute(
            f"""
            SELECT a.*,e.start_time FROM sports_player_appearances a
            JOIN sports_events e ON e.id=a.event_id
            WHERE 1=1{player_league_filter}
            ORDER BY e.start_time,a.event_id,a.player_id
            """,  # noqa: S608 - filter is a fixed internal fragment
            parameters,
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
                    team["outcomes"].append(
                        (str(event["start_time"]), side_score > opponent_score)
                    )

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
        history = _rate_history(outcomes, (wins, losses))
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
                "rank_score": (wins + 4) / (games + 8),
            }
        )
    team_rows.sort(
        key=lambda row: (-row["rank_score"], -row["games"], row["abbreviation"])
    )

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
        history = _rate_history(outcomes)
        player_output.append(
            {
                **player,
                "wins": wins,
                "losses": games - wins,
                "games": games,
                "win_rate": round(wins / games * 100, 1),
                "trend_pct": round(history[-1] - history[0], 1) if len(history) > 1 else 0.0,
                "history": history,
                "rank_score": (wins + 2) / (games + 4),
            }
        )
    player_output.sort(
        key=lambda row: (-row["rank_score"], -row["games"], row["name"])
    )
    return {
        "teams": team_rows[: max(1, min(limit, 100))],
        "players": player_output[: max(1, min(limit, 100))],
        "player_min_games": 3,
        "league": selected_league,
        "leagues": [{"key": key, "name": value["name"]} for key, value in LEAGUES.items()],
        "updated_at": _iso(),
    }


def sports_event(event_id: str) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute("SELECT * FROM sports_events WHERE id=?", (event_id,)).fetchone()
        if not row:
            return None
        event = _event_row(database, row)
        odds_rows = database.execute(
            """
            SELECT * FROM sports_odds_snapshots WHERE event_id=?
            ORDER BY observed_at DESC,id DESC LIMIT 30
            """,
            (event_id,),
        ).fetchall()
        event["odds_history"] = [dict(item) for item in reversed(odds_rows)]
        pick_rows = database.execute(
            """
            SELECT p.*,ci.handle AS caller_handle FROM sports_picks p
            JOIN caller_identities ci ON ci.id=p.caller_identity_id
            WHERE p.event_id=? ORDER BY p.created_at DESC LIMIT 50
            """,
            (event_id,),
        ).fetchall()
        event["picks"] = [dict(item) for item in pick_rows]
        news_rows = database.execute(
            """
            SELECT * FROM sports_news_articles WHERE event_id=?
            ORDER BY published_at DESC,id DESC LIMIT 8
            """,
            (event_id,),
        ).fetchall()
        event["news"] = [dict(item) for item in news_rows]
    return event


def create_sports_pick(
    user_id: str,
    event_id: str,
    selection: str,
) -> dict[str, Any]:
    if selection not in {"home", "away"}:
        raise ValueError("Pick must be home or away")
    timestamp = _iso()
    with connection() as database:
        identity = ensure_caller_identity_with_database(database, user_id)
        event = database.execute(
            "SELECT * FROM sports_events WHERE id=? AND status='pre' AND start_time>?",
            (event_id, timestamp),
        ).fetchone()
        if not event:
            raise ValueError("This game is no longer open for picks")
        odds = _latest_odds(database, event_id)
        if not odds:
            raise ValueError("Moneyline odds are not available")
        american_odds = odds[f"{selection}_odds"]
        if american_odds is None:
            raise ValueError("Moneyline odds are not available for that side")
        existing = database.execute(
            """
            SELECT * FROM sports_picks
            WHERE user_id=? AND event_id=? AND market='moneyline'
            """,
            (user_id, event_id),
        ).fetchone()
        if existing:
            return dict(existing)
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
                pick_id, public_id, user_id, identity["id"], event_id, selection, None,
                int(american_odds), odds.get("sportsbook") or "Unknown",
                odds["observed_at"], prediction.get("id") if prediction else None,
                timestamp, timestamp,
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
