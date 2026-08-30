from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from runner_watch.ingestion import SourceFetch
from runner_web.db import connection
from runner_web.ingestion import record_source_fetch

PROVIDER = "the-odds-api"
FEED = "sports_moneyline_odds"
API_ROOT = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {
    "mlb": "baseball_mlb",
    "nfl": "americanfootball_nfl",
    "nba": "basketball_nba",
    "nhl": "icehockey_nhl",
}
REFRESH_SLOTS = (
    ("opening", timedelta(hours=36)),
    ("pregame", timedelta(hours=6)),
    ("close", timedelta(minutes=90)),
)
SPORTS_DAY_TIMEZONE = ZoneInfo("America/New_York")
CONSENSUS_BOOKMAKER_KEY = "consensus"
CONSENSUS_SPORTSBOOK = "Market consensus"
MIN_CONSENSUS_BOOKS = 3
BOOKMAKER_FRESHNESS = timedelta(minutes=20)
BOOKMAKER_MAX_AGE = timedelta(hours=2)
BOOKMAKER_FUTURE_TOLERANCE = timedelta(minutes=5)
EVENT_START_TOLERANCE = timedelta(hours=2)


class OddsApiError(RuntimeError):
    """A safe error that never includes the API key."""

    def __init__(self, message: str, quota: Quota | None = None) -> None:
        super().__init__(message)
        self.quota = quota


@dataclass(frozen=True, slots=True)
class OddsApiConfig:
    api_key: str
    enabled: bool = True
    region: str = "us"
    bookmakers: tuple[str, ...] = ()
    preferred_bookmakers: tuple[str, ...] = ()
    working_limit: int = 450
    reserve_credits: int = 50
    retry_seconds: int = 1_800

    @classmethod
    def from_env(cls, api_key: str | None = None) -> OddsApiConfig:
        enabled = os.getenv("ODDS_API_ENABLED", "true").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        bookmakers = _csv_env("ODDS_API_BOOKMAKERS")
        if len(bookmakers) > 10:
            raise ValueError("ODDS_API_BOOKMAKERS must contain at most 10 bookmakers")
        region = os.getenv("ODDS_API_REGION", "us").strip().lower() or "us"
        if "," in region:
            raise ValueError("ODDS_API_REGION must contain exactly one region")
        working_limit = max(1, int(os.getenv("ODDS_API_MONTHLY_WORKING_LIMIT", "450")))
        reserve = max(0, int(os.getenv("ODDS_API_RESERVE_CREDITS", "50")))
        if working_limit + reserve > 500:
            raise ValueError("The Odds API working limit plus reserve cannot exceed 500")
        return cls(
            api_key=(api_key or os.getenv("ODDS_API_KEY", "")).strip(),
            enabled=enabled,
            region=region,
            bookmakers=bookmakers,
            preferred_bookmakers=_csv_env("ODDS_API_PREFERRED_BOOKMAKERS"),
            working_limit=working_limit,
            reserve_credits=reserve,
            retry_seconds=max(300, int(os.getenv("ODDS_API_RETRY_SECONDS", "1800"))),
        )

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.api_key)


@dataclass(frozen=True, slots=True)
class Quota:
    used: int
    remaining: int
    last: int


@dataclass(frozen=True, slots=True)
class RefreshDecision:
    league: str
    slate: str
    slot: str


@dataclass(frozen=True, slots=True)
class OddsFetchResult:
    moneylines: tuple[dict[str, Any], ...]
    quota: Quota
    locator: str


def _csv_env(name: str) -> tuple[str, ...]:
    values: list[str] = []
    for raw in os.getenv(name, "").split(","):
        value = raw.strip().lower()
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _header_int(headers: Any, name: str) -> int | None:
    value = headers.get(name)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _quota_from_headers(headers: Any) -> Quota:
    used = _header_int(headers, "x-requests-used")
    remaining = _header_int(headers, "x-requests-remaining")
    last = _header_int(headers, "x-requests-last")
    if used is None or remaining is None or last is None:
        raise OddsApiError("The Odds API response did not include complete quota headers")
    return Quota(used=used, remaining=remaining, last=last)


def _request_json(url: str) -> tuple[Any, Quota]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "RATi-Sports/0.1 https://sports.rati.chat"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            body = response.read()
            quota = _quota_from_headers(response.headers)
    except urllib.error.HTTPError as exc:
        try:
            error_quota = _quota_from_headers(exc.headers)
        except OddsApiError:
            error_quota = None
        raise OddsApiError(f"The Odds API returned HTTP {exc.code}", error_quota) from None
    except (TimeoutError, urllib.error.URLError):
        raise OddsApiError("The Odds API request failed") from None
    try:
        return json.loads(body), quota
    except (TypeError, ValueError):
        raise OddsApiError("The Odds API returned invalid JSON", quota) from None


def probe_quota(config: OddsApiConfig) -> Quota:
    if not config.active:
        raise OddsApiError("The Odds API is not configured")
    url = f"{API_ROOT}/sports/?{urlencode({'apiKey': config.api_key})}"
    payload, quota = _request_json(url)
    if not isinstance(payload, list):
        raise OddsApiError("The Odds API sports response had an unexpected shape")
    record_quota(quota)
    return quota


def can_spend(config: OddsApiConfig, quota: Quota) -> bool:
    return quota.used < config.working_limit and quota.remaining > config.reserve_credits


def _implied_probability(american_odds: int) -> float:
    if american_odds > 0:
        return 100 / (american_odds + 100)
    return -american_odds / (-american_odds + 100)


def _no_vig_probabilities(home_odds: int, away_odds: int) -> tuple[float, float]:
    home = _implied_probability(home_odds)
    away = _implied_probability(away_odds)
    total = home + away
    return home / total, away / total


def _american_from_probability(probability: float) -> int:
    bounded = max(0.01, min(0.99, probability))
    if bounded >= 0.5:
        return -round(100 * bounded / (1 - bounded))
    return round(100 * (1 - bounded) / bounded)


def _moneyline_from_bookmaker(
    bookmaker: dict[str, Any], home_team: str, away_team: str
) -> dict[str, Any] | None:
    market = next(
        (item for item in bookmaker.get("markets") or [] if item.get("key") == "h2h"),
        None,
    )
    if not market:
        return None
    prices = {
        str(outcome.get("name") or ""): outcome.get("price")
        for outcome in market.get("outcomes") or []
    }
    try:
        home_odds = int(prices[home_team])
        away_odds = int(prices[away_team])
    except (KeyError, TypeError, ValueError):
        return None
    if not home_odds or not away_odds:
        return None
    home_probability, away_probability = _no_vig_probabilities(home_odds, away_odds)
    return {
        "sportsbook": str(bookmaker.get("title") or bookmaker.get("key") or "Unknown"),
        "sportsbook_key": str(bookmaker.get("key") or ""),
        "home_odds": home_odds,
        "away_odds": away_odds,
        "home_probability": home_probability,
        "away_probability": away_probability,
        "last_update": str(market.get("last_update") or bookmaker.get("last_update") or ""),
    }


def _select_bookmaker(
    candidates: list[dict[str, Any]], preferred: tuple[str, ...]
) -> dict[str, Any] | None:
    if not candidates:
        return None
    preference = {name: index for index, name in enumerate(preferred)}

    def rank(line: dict[str, Any]) -> tuple[int, float, str]:
        key = str(line["sportsbook_key"]).lower()
        title = str(line["sportsbook"]).lower()
        preferred_rank = min(
            (preference[name] for name in (key, title) if name in preference),
            default=len(preference),
        )
        updated = _parse_time(line["last_update"])
        timestamp = updated.timestamp() if updated else 0.0
        return preferred_rank, -timestamp, key

    return min(candidates, key=rank)


def _fresh_moneylines(
    candidates: list[dict[str, Any]], observed_at: datetime | None = None
) -> list[dict[str, Any]]:
    """Return quotes that are recent now and synchronized with one another."""

    reference = (observed_at or datetime.now(UTC)).astimezone(UTC)
    recent = [
        (line, updated)
        for line in candidates
        if (updated := _parse_time(line.get("last_update"))) is not None
        and -BOOKMAKER_FUTURE_TOLERANCE <= reference - updated <= BOOKMAKER_MAX_AGE
    ]
    if not recent:
        return []
    newest = max(updated for _line, updated in recent)
    return [
        line
        for line, updated in recent
        if newest - updated <= BOOKMAKER_FRESHNESS
    ]


def _consensus_moneyline(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(candidates) < MIN_CONSENSUS_BOOKS:
        return None
    home_probability = float(median(line["home_probability"] for line in candidates))
    away_probability = 1 - home_probability
    updates = [
        parsed
        for line in candidates
        if (parsed := _parse_time(line.get("last_update"))) is not None
    ]
    return {
        "sportsbook": CONSENSUS_SPORTSBOOK,
        "sportsbook_key": CONSENSUS_BOOKMAKER_KEY,
        "home_odds": _american_from_probability(home_probability),
        "away_odds": _american_from_probability(away_probability),
        "home_probability": home_probability,
        "away_probability": away_probability,
        "last_update": max(updates).isoformat() if updates else "",
        "bookmaker_count": len(candidates),
    }


def normalize_moneylines(
    payload: Any,
    preferred: tuple[str, ...] = (),
    observed_at: datetime | None = None,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, list):
        raise OddsApiError("The Odds API odds response had an unexpected shape")
    moneylines: list[dict[str, Any]] = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        home_team = str(event.get("home_team") or "")
        away_team = str(event.get("away_team") or "")
        candidates = [
            line
            for bookmaker in event.get("bookmakers") or []
            if (line := _moneyline_from_bookmaker(bookmaker, home_team, away_team)) is not None
        ]
        fresh = _fresh_moneylines(candidates, observed_at)
        selected = _select_bookmaker(fresh, preferred)
        if not selected:
            continue
        moneylines.append(
            {
                "external_id": str(event.get("id") or ""),
                "home_team": str(event.get("home_team") or ""),
                "away_team": str(event.get("away_team") or ""),
                "start_time": _parse_time(event.get("commence_time")),
                "bookmakers": tuple(fresh),
                "consensus": _consensus_moneyline(fresh),
                "preferred": selected,
            }
        )
    return tuple(moneylines)


def fetch_moneylines(config: OddsApiConfig, league: str) -> OddsFetchResult:
    if not config.active:
        raise OddsApiError("The Odds API is not configured")
    try:
        sport_key = SPORT_KEYS[league]
    except KeyError:
        raise ValueError("Unsupported league") from None
    parameters = {
        "apiKey": config.api_key,
        "markets": "h2h",
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    safe_parameters = {key: value for key, value in parameters.items() if key != "apiKey"}
    if config.bookmakers:
        parameters["bookmakers"] = ",".join(config.bookmakers)
        safe_parameters["bookmakers"] = parameters["bookmakers"]
    else:
        parameters["regions"] = config.region
        safe_parameters["regions"] = config.region
    locator = f"{API_ROOT}/sports/{sport_key}/odds?{urlencode(safe_parameters)}"
    url = f"{API_ROOT}/sports/{sport_key}/odds?{urlencode(parameters)}"
    started = datetime.now(UTC)
    quota: Quota | None = None
    try:
        payload, quota = _request_json(url)
        record_quota(quota)
        moneylines = normalize_moneylines(
            payload,
            config.preferred_bookmakers,
            observed_at=datetime.now(UTC),
        )
        record_source_fetch(
            SourceFetch.success(
                source=PROVIDER,
                feed=FEED,
                locator=locator,
                started_at=started,
                payload={
                    "league": league,
                    "event_count": len(payload) if isinstance(payload, list) else 0,
                    "moneyline_count": len(moneylines),
                    "bookmaker_line_count": sum(
                        len(event.get("bookmakers") or ()) for event in moneylines
                    ),
                },
                content_type="application/json",
                metadata={
                    "league": league,
                    "received_count": len(moneylines),
                    "credits_last": quota.last,
                    "credits_remaining": quota.remaining,
                },
            )
        )
        return OddsFetchResult(moneylines=moneylines, quota=quota, locator=locator)
    except Exception as exc:
        if isinstance(exc, OddsApiError):
            safe_error = exc
            if safe_error.quota is None and quota is not None:
                safe_error = OddsApiError(str(safe_error), quota)
        else:
            safe_error = OddsApiError("Odds parsing failed", quota)
        if safe_error.quota is not None:
            record_quota(safe_error.quota)
        record_source_fetch(
            SourceFetch.failure(
                source=PROVIDER,
                feed=FEED,
                locator=locator,
                started_at=started,
                error=safe_error,
                metadata={"league": league},
            )
        )
        raise safe_error from None


def _team_key(value: Any) -> str:
    key = "".join(character for character in str(value or "").lower() if character.isalnum())
    aliases = {"laclippers": "losangelesclippers"}
    return aliases.get(key, key)


def apply_moneylines(
    events: list[dict[str, Any]], moneylines: tuple[dict[str, Any], ...]
) -> int:
    """Attach each provider market to at most one scheduled event."""

    available = list(enumerate(moneylines))
    used_markets: set[int] = set()
    matched = 0
    for event in sorted(
        events,
        key=lambda item: _parse_time(item.get("start_time")) or datetime.max.replace(tzinfo=UTC),
    ):
        key = (_team_key(event["home"]["name"]), _team_key(event["away"]["name"]))
        event_start = _parse_time(event.get("start_time"))
        event_external_id = str(event.get("external_id") or "")
        candidates: list[tuple[timedelta, int, dict[str, Any]]] = []
        for index, line in available:
            if index in used_markets:
                continue
            line_key = (_team_key(line["home_team"]), _team_key(line["away_team"]))
            if line_key != key:
                continue
            market_start = _parse_time(line.get("start_time"))
            if event_external_id and event_external_id == str(line.get("external_id") or ""):
                candidates.append((timedelta(0), index, line))
            elif event_start is not None and market_start is not None:
                difference = abs(event_start - market_start)
                if difference <= EVENT_START_TOLERANCE:
                    candidates.append((difference, index, line))
        if not candidates:
            continue
        _difference, market_index, market = min(
            candidates,
            key=lambda item: (item[0], str(item[2].get("external_id") or "")),
        )
        if not market:
            continue
        consensus = market.get("consensus")
        preferred = market.get("preferred")
        line = consensus or preferred
        bookmakers = list(market.get("bookmakers") or ())
        if not line or not preferred:
            continue
        event.update(
            {
                "odds_provider": PROVIDER,
                "sportsbook": line["sportsbook"],
                "home_odds": line["home_odds"],
                "away_odds": line["away_odds"],
                "home_open_odds": None,
                "away_open_odds": None,
                "spread": None,
                "total": None,
                "market_book_count": len(bookmakers),
                "market_is_consensus": consensus is not None,
                "bookmaker_moneylines": bookmakers,
                "preferred_sportsbook": preferred["sportsbook"],
            }
        )
        used_markets.add(market_index)
        matched += 1
    return matched


def clear_event_odds(events: list[dict[str, Any]]) -> None:
    for event in events:
        event.update(
            {
                "odds_provider": None,
                "sportsbook": "",
                "home_odds": None,
                "away_odds": None,
                "home_open_odds": None,
                "away_open_odds": None,
                "spread": None,
                "total": None,
                "market_book_count": 0,
                "market_is_consensus": False,
                "bookmaker_moneylines": [],
                "preferred_sportsbook": "",
            }
        )


def _state_value(key: str) -> dict[str, Any]:
    with connection() as database:
        row = database.execute("SELECT value FROM worker_state WHERE key=?", (key,)).fetchone()
    if not row:
        return {}
    try:
        value = json.loads(str(row["value"]))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(key: str, value: dict[str, Any], at: datetime | None = None) -> None:
    timestamp = (at or datetime.now(UTC)).isoformat()
    with connection() as database:
        database.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, separators=(",", ":"), sort_keys=True), timestamp),
        )


def record_quota(quota: Quota, at: datetime | None = None) -> None:
    _write_state(
        "odds_api_quota",
        {"used": quota.used, "remaining": quota.remaining, "last": quota.last},
        at,
    )


def last_recorded_quota() -> Quota | None:
    saved = _state_value("odds_api_quota")
    try:
        return Quota(
            used=int(saved["used"]),
            remaining=int(saved["remaining"]),
            last=int(saved["last"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def refresh_decision(
    league: str,
    events: list[dict[str, Any]],
    at: datetime,
    state: dict[str, Any] | None = None,
    retry_seconds: int = 1_800,
) -> RefreshDecision | None:
    current = at.astimezone(UTC)
    upcoming = sorted(
        (
            event["start_time"].astimezone(UTC)
            for event in events
            if event.get("status") == "pre"
            and isinstance(event.get("start_time"), datetime)
            and event["start_time"].astimezone(UTC) > current
        ),
    )
    if not upcoming:
        return None
    until_start = upcoming[0] - current
    eligible = [name for name, window in REFRESH_SLOTS if until_start <= window]
    if not eligible:
        return None
    slot = eligible[-1]
    slate = upcoming[0].astimezone(SPORTS_DAY_TIMEZONE).date().isoformat()
    saved = state if state is not None else _state_value(f"odds_api_refresh:{league}")
    if saved.get("slate") != slate:
        saved = {}
    completed = set(saved.get("completed") or [])
    if slot in completed:
        return None
    last_attempt = _parse_time(saved.get("last_attempt_at"))
    if (
        saved.get("last_attempt_slot") == slot
        and last_attempt is not None
        and (current - last_attempt).total_seconds() < retry_seconds
    ):
        return None
    return RefreshDecision(league=league, slate=slate, slot=slot)


def mark_refresh_attempt(
    decision: RefreshDecision, *, successful: bool, at: datetime | None = None
) -> None:
    current = (at or datetime.now(UTC)).astimezone(UTC)
    key = f"odds_api_refresh:{decision.league}"
    saved = _state_value(key)
    if saved.get("slate") != decision.slate:
        saved = {"slate": decision.slate, "completed": []}
    completed = set(saved.get("completed") or [])
    if successful:
        selected_index = next(
            index for index, (name, _window) in enumerate(REFRESH_SLOTS) if name == decision.slot
        )
        completed.update(name for name, _window in REFRESH_SLOTS[: selected_index + 1])
    saved.update(
        {
            "completed": sorted(completed),
            "last_attempt_at": current.isoformat(),
            "last_attempt_slot": decision.slot,
        }
    )
    _write_state(key, saved, current)
