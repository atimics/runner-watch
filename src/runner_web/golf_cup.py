"""Saved Cup pairings, world rankings, and a reproducible ranking forecast."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from runner_watch.ingestion import SourceFetch
from runner_web.db import connection
from runner_web.ingestion import record_source_fetch

EVENT_ID = "golf:401824815"
MODEL_VERSION = "cup-ranking-v1"
MATCH_URL = "https://www.espn.com/golf/leaderboard?tournamentId=401824815"
ROSTER_URL = "https://www.presidentscup.com/teams"
RANK_URL = (
    "https://site.web.api.espn.com/apis/site/v2/sports/golf/all/rankings?region=us&lang=en&polls=1"
)
TEAM_NAMES = {"1": "USA", "3": "International"}
MATCH_TIE_RATE = 0.12
MAX_BYTES = 8_000_000


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _name(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value).lower() if c.isalnum())


def parse_matches(html: str) -> dict[str, Any]:
    marker = "window['__espnfitt__']="
    offset = html.index(marker) + len(marker)
    state, _ = json.JSONDecoder().raw_decode(html[offset:])
    match_data = state["page"]["content"]["leaderboard"]["mtch"]
    header = match_data["hdr"]
    if "~e:401824815~" not in header.get("uid", ""):
        raise ValueError("Unexpected Cup event")
    totals = {str(team["teamId"]): _number(team.get("score")) for team in header["competitors"]}
    if set(totals) != set(TEAM_NAMES) or any(value is None for value in totals.values()):
        raise ValueError("Cup team points are incomplete")
    if any(value < 0 or value > 30 or value * 2 != int(value * 2) for value in totals.values()):
        raise ValueError("Invalid Cup team points")
    if sum(totals.values()) > 30 or not sum(totals.values()).is_integer():
        raise ValueError("Cup point totals are inconsistent")
    matches = []
    seen = set()
    for group in match_data.get("grps", []):
        for raw in group.get("competitions", []):
            match_id = str(raw["id"])
            if match_id in seen:
                continue
            seen.add(match_id)
            sides: dict[str, dict[str, Any]] = {}
            for player in raw.get("competitors", []):
                team_id = str(player.get("teamId", ""))
                if team_id not in TEAM_NAMES:
                    raise ValueError("Unexpected Cup pairing team")
                side = sides.setdefault(team_id, {"players": [], "winner": False})
                name = str(player.get("displayName") or "").strip()
                if name and name not in side["players"]:
                    side["players"].append(name)
                side.update(
                    winner=side["winner"] or bool(player.get("winner")),
                    score=str(player.get("score") or "—"),
                    holes_remaining=player.get("holesRemaining"),
                )
            if set(sides) != set(TEAM_NAMES) or any(not side["players"] for side in sides.values()):
                raise ValueError("Cup pairing names are incomplete")
            status = raw.get("status", {})
            matches.append(
                {
                    "id": match_id,
                    "session": str(group.get("name") or raw.get("name") or "Match play"),
                    "start_time": str(raw.get("date") or ""),
                    "state": str(status.get("state") or raw.get("statusState") or "pre"),
                    "completed": bool(raw.get("completed")) or status.get("state") == "post",
                    "status": str(status.get("detail") or status.get("shortDetail") or "Scheduled"),
                    "sides": sides,
                }
            )
    return {
        "points": totals,
        "state": header.get("statusState", "pre"),
        "completed": bool(header.get("completed")),
        "matches": matches,
    }


class _RosterCells(HTMLParser):
    """Read the official roster's two columns, including split first/last names."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.parts: list[str] = []
        self.cells: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif dict(attrs).get("data-valign") == "middle":
                self.depth = 1
                self.parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                self.cells.append(" ".join(" ".join(self.parts).split()))

    def handle_data(self, data: str) -> None:
        if self.depth:
            self.parts.append(data)


def parse_rosters(html: str) -> dict[str, list[str]]:
    parser = _RosterCells()
    parser.feed(html)
    cells = parser.cells
    teams: dict[str, list[str]] = {"1": [], "3": []}
    for label in ["Automatic Qualifiers", "Captain’s Picks"]:
        us_header = f"2026 U.S. Team {label}"
        intl_header = f"2026 INT Team {label}"
        start = cells.index(us_header)
        if cells[start + 1] != intl_header:
            raise ValueError("Cup roster columns changed")
        for index in range(start + 2, start + 38, 6):
            row = cells[index : index + 6]
            if len(row) != 6 or row[0] or row[3] or any(not row[n] for n in [1, 2, 4, 5]):
                raise ValueError("Cup roster row changed")
            teams["1"].append(f"{row[1]} {row[2]}")
            teams["3"].append(f"{row[4]} {row[5]}")
    if len({_name(name) for names in teams.values() for name in names}) != 24:
        raise ValueError("Cup requires 24 distinct roster players")
    return teams


def parse_rankings(payload: dict[str, Any]) -> dict[str, Any]:
    ranking = next(row for row in payload["rankings"] if row.get("type") == "WORLDRANK")
    players = {}
    for row in ranking["ranks"]:
        athlete = row.get("athlete") or {}
        name = str(athlete.get("displayName") or "")
        stats = {item["name"]: _number(item.get("value")) for item in row.get("recordStats", [])}
        average = stats.get("averagePoints")
        rank = _number(row.get("current"))
        if not name or average is None or average <= 0 or rank is None or rank < 1:
            continue
        players[_name(name)] = {
            "id": str(athlete.get("id") or ""),
            "name": name,
            "rank": int(rank),
            "average_points": average,
            "points_gained": stats.get("gainedPoints"),
            "events": stats.get("totalEvents"),
        }
    if not players:
        raise ValueError("World ranking points are pending")
    updated = str(ranking["update"]).replace("/", "-")
    datetime.fromisoformat(updated)
    return {"updated_at": updated, "players": players}


def _strength(players: list[dict[str, Any]]) -> float:
    # Square roots soften ranking-point gaps before the Bradley-Terry comparison.
    return sum(math.sqrt(player["average_points"]) for player in players) / len(players)


def _cup_distribution(probabilities: list[float], us_points: float) -> dict[str, float]:
    # Exact convolution in half-point units, so the same inputs always replay exactly.
    distribution = {int(round(us_points * 2)): 1.0}
    for probability in probabilities:
        following: dict[int, float] = defaultdict(float)
        for score, mass in distribution.items():
            following[score] += mass * (1 - MATCH_TIE_RATE) * (1 - probability)
            following[score + 1] += mass * MATCH_TIE_RATE
            following[score + 2] += mass * (1 - MATCH_TIE_RATE) * probability
        distribution = following
    return {
        "usa": sum(mass for score, mass in distribution.items() if score > 30),
        "international": sum(mass for score, mass in distribution.items() if score < 30),
        "tie": distribution.get(30, 0.0),
    }


def build_analysis(sources: dict[str, Any]) -> dict[str, Any]:
    """Join named roster players to ranking points; keep incomplete evidence visible."""
    matches = sources.get("matches", {}).get("data", {})
    ranks = sources.get("rankings", {}).get("data", {})
    rosters = sources.get("rosters", {}).get("data", {})
    ranked = ranks.get("players", {})
    analysis: dict[str, Any] = {
        "model_version": MODEL_VERSION,
        "teams": [],
        "matches": [],
        "prediction": None,
        "ranking_date": ranks.get("updated_at", ""),
        "points": matches.get("points", {}),
        "state": matches.get("state", "pre"),
        "match_time": sources.get("matches", {}).get("captured_at", ""),
        "sources": [
            {key: value for key, value in source.items() if key != "data"}
            for source in sources.values()
        ],
    }
    player_map = {}
    for team_id, team_name in TEAM_NAMES.items():
        players = []
        for name in rosters.get(team_id, []):
            player = {
                "name": name,
                "rank": None,
                "average_points": None,
                **ranked.get(_name(name), {}),
            }
            players.append(player)
            player_map[_name(name)] = (team_id, player)
        players.sort(key=lambda item: item["rank"] or 100_000)
        covered = [player for player in players if player["average_points"] is not None]
        analysis["teams"].append(
            {
                "id": team_id,
                "name": team_name,
                "players": players,
                "covered": len(covered),
                "average_points": sum(p["average_points"] for p in covered) / len(covered)
                if covered
                else None,
                "top_twenty": sum(p["rank"] <= 20 for p in covered),
            }
        )
    complete = all(len(team["players"]) == team["covered"] == 12 for team in analysis["teams"])
    team_strength = {
        team["id"]: _strength(team["players"]) for team in analysis["teams"] if complete
    }
    upcoming = []
    for raw in matches.get("matches", []):
        match = {**raw, "teams": [], "forecast": None}
        strengths = {}
        for team_id, team_name in TEAM_NAMES.items():
            side = raw["sides"][team_id]
            players = [player_map.get(_name(name)) for name in side["players"]]
            if players and all(p and p[0] == team_id and p[1]["average_points"] for p in players):
                strengths[team_id] = _strength([p[1] for p in players])
            match["teams"].append({"id": team_id, "name": team_name, **side})
        if len(strengths) == 2:
            us_probability = strengths["1"] / sum(strengths.values())
            match["forecast"] = {
                "usa": (1 - MATCH_TIE_RATE) * us_probability,
                "international": (1 - MATCH_TIE_RATE) * (1 - us_probability),
                "tie": MATCH_TIE_RATE,
            }
        if not raw["completed"]:
            upcoming.append(match)
        analysis["matches"].append(match)
    if not complete or set(matches.get("points", {})) != set(TEAM_NAMES):
        return analysis
    us_points, intl_points = (matches["points"][key] for key in ["1", "3"])
    remaining = int(30 - us_points - intl_points)
    finished = sum(match["completed"] for match in analysis["matches"])
    if (
        remaining < len(upcoming)
        or finished > us_points + intl_points
        or any(match["forecast"] is None for match in upcoming)
    ):
        return analysis
    # Use ranking estimates while an individual match is in progress.
    baseline = team_strength["1"] / sum(team_strength.values())
    probabilities = [match["forecast"]["usa"] / (1 - MATCH_TIE_RATE) for match in upcoming]
    probabilities += [baseline] * (remaining - len(probabilities))
    chance = _cup_distribution(probabilities, us_points)
    expected_us = us_points + sum(
        (1 - MATCH_TIE_RATE) * p + MATCH_TIE_RATE / 2 for p in probabilities
    )
    if us_points > 15:
        chance = {"usa": 1.0, "international": 0.0, "tie": 0.0}
    elif intl_points > 15:
        chance = {"usa": 0.0, "international": 1.0, "tie": 0.0}
    winner = "USA" if chance["usa"] > chance["international"] else "International"
    if math.isclose(chance["usa"], chance["international"], abs_tol=1e-12):
        winner = "Even matchup"
    if chance["tie"] == 1:
        winner = "Cup tied"
    analysis["prediction"] = {
        **chance,
        "winner": winner,
        "winner_probability": chance["tie"]
        if winner == "Cup tied"
        else max(chance["usa"], chance["international"]),
        "expected_usa": expected_us,
        "expected_international": 30 - expected_us,
        "remaining": remaining,
        "announced_remaining": len(upcoming),
        "roster_average_matches": remaining - len(upcoming),
        "settled": us_points > 15 or intl_points > 15 or remaining == 0,
    }
    return analysis


def _fetch_source(key: str, url: str, parser: Any, now: datetime) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "RATi Sports/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Golf source exceeded the size limit")
    data = parser(raw.decode("utf-8"))
    if key == "rankings" and datetime.fromisoformat(data["updated_at"]).date() > now.date():
        raise ValueError("World ranking date is ahead of the collection date")
    return {
        "key": key,
        "url": url,
        "captured_at": now.isoformat(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "data": data,
    }


def latest_snapshot() -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            "SELECT snapshot_json FROM sports_golf_analysis "
            "WHERE event_id=? ORDER BY captured_at DESC LIMIT 1",
            (EVENT_ID,),
        ).fetchone()
    return json.loads(row["snapshot_json"]) if row else None


def refresh_cup_analysis(now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    previous = latest_snapshot() or {}
    sources = dict(previous.get("sources", {}))
    errors = []
    for key, url, parser, ttl in [
        ("matches", MATCH_URL, parse_matches, timedelta(minutes=5)),
        ("rosters", ROSTER_URL, parse_rosters, timedelta(hours=6)),
        ("rankings", RANK_URL, lambda text: parse_rankings(json.loads(text)), timedelta(hours=6)),
    ]:
        cached = sources.get(key)
        roster_changed = False
        if key == "rankings" and cached and "rosters" in sources:
            wanted = {_name(name) for team in sources["rosters"]["data"].values() for name in team}
            roster_changed = not wanted.issubset(cached["data"]["players"])
        if (
            cached
            and not roster_changed
            and timedelta(0) <= current - datetime.fromisoformat(cached["captured_at"]) < ttl
        ):
            continue
        try:
            source = _fetch_source(key, url, parser, current)
            sources[key] = source
            record_source_fetch(
                SourceFetch.success(
                    source="espn" if key != "rosters" else "presidentscup",
                    feed=f"sports_golf_cup_{key}",
                    locator=url,
                    started_at=current,
                    payload=source,
                    content_type="application/json",
                )
            )
        except Exception as exc:
            errors.append(key)
            record_source_fetch(
                SourceFetch.failure(
                    source="espn" if key != "rosters" else "presidentscup",
                    feed=f"sports_golf_cup_{key}",
                    locator=url,
                    started_at=current,
                    error=exc,
                )
            )
    # Keep only roster players in the saved model input; source receipts hold the full ranking.
    if "rankings" in sources and "rosters" in sources:
        wanted = {_name(name) for team in sources["rosters"]["data"].values() for name in team}
        ranking = sources["rankings"]
        sources["rankings"] = {
            **ranking,
            "data": {
                **ranking["data"],
                "players": {
                    key: value for key, value in ranking["data"]["players"].items() if key in wanted
                },
            },
        }
    analysis = build_analysis(sources)
    snapshot = {
        "captured_at": current.isoformat(),
        "sources": sources,
        "errors": errors,
        "analysis": analysis,
        "model_version": MODEL_VERSION,
    }
    digest = hashlib.sha256(_json(snapshot).encode()).hexdigest()
    with connection() as database:
        database.execute(
            "INSERT INTO sports_golf_analysis(event_id,input_hash,captured_at,snapshot_json) "
            "VALUES(?,?,?,?) ON CONFLICT(event_id,input_hash) DO NOTHING",
            (EVENT_ID, digest, current.isoformat(), _json(snapshot)),
        )
    return {
        "matches": len(analysis["matches"]),
        "players": sum(t["covered"] for t in analysis["teams"]),
        "errors": errors,
    }


def saved_cup_analysis(now: datetime | None = None) -> dict[str, Any] | None:
    snapshot = latest_snapshot()
    if snapshot is None:
        return None
    analysis = snapshot["analysis"]
    current = now or datetime.now(UTC)
    match_time = analysis.get("match_time")
    analysis["stale"] = not match_time or current - datetime.fromisoformat(match_time) > timedelta(
        minutes=30
    )
    analysis["refresh_pending"] = bool(snapshot.get("errors"))
    if analysis.get("ranking_date"):
        rank_date = datetime.fromisoformat(analysis["ranking_date"]).replace(tzinfo=UTC)
        analysis["stale"] = analysis["stale"] or current - rank_date > timedelta(days=14)
    analysis["captured_at"] = snapshot["captured_at"]
    return analysis
