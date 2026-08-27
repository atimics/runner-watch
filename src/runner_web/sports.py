from __future__ import annotations

import hashlib
import json
import math
import secrets
import urllib.request
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from runner_watch.ingestion import SourceFetch
from runner_web.caller_ids import ensure_caller_identity_with_database
from runner_web.db import connection
from runner_web.ingestion import record_source_fetch

MODEL_VERSION = "team-form-v1"
SOURCE = "espn"
FEED = "sports_scoreboard_preview"
SOURCE_URL = "https://site.api.espn.com/apis/site/v2/sports"
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
    errors: dict[str, str] = {}
    for league in LEAGUES:
        try:
            events = fetch_league(league, at)
            counts[league] = store_events(events)
        except Exception as exc:
            errors[league] = str(exc)[:240]
    settled = settle_picks()
    return {"counts": counts, "errors": errors, "settled": settled, "model": MODEL_VERSION}


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

    if (
        not prediction
        or prediction.get("edge") is None
        or not (side := _preferred_side(prediction))
    ):
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
        "dot_y": coordinates[0].split(",", 1)[1],
        "current_pct": current,
        "change_pct": change,
        "label": f"{team} model edge {movement}; now {current:+.1f} percentage points",
    }


def _event_row(database: Any, row: Any) -> dict[str, Any]:
    item = dict(row)
    item["odds"] = _latest_odds(database, str(item["id"]))
    item["prediction"] = _latest_prediction(database, str(item["id"]))
    item["edge_history"] = _edge_sparkline(database, item, item["prediction"])
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
