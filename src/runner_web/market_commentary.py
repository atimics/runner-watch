from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from runner_web.ai_kol import FLASH, actor_snapshot
from runner_web.db import connection
from runner_web.market_forecasts import attach_market_forecasts, forecast_record
from runner_web.pseudonyms import comment_avatar_ability, comment_avatar_profile

EASTERN = ZoneInfo("America/New_York")
CONTRACT_VERSION = "market-report-desk-v1"
MAX_ATTEMPTS = 3
VOICES_PER_REPORT = 3
COMMENT_MAX_CHARS = 240
NARRATIVE_MAX_CHARS = 900
HEADLINE_MAX_CHARS = 120
POINT_MAX_CHARS = 160
MAX_POINTS = 4
SETTLEMENT_WAIT_UNTIL = clock_time(17, 30)

REPORT_VOICES: tuple[dict[str, str], ...] = (
    {
        "id": "amber-scout",
        "ability_id": "catalyst_scout",
        "name": "Alert Amber Scout",
        "seed": "6f2a1c9d4b8e35770ac1d5e2f39b8046",
    },
    {
        "id": "obsidian-sentinel",
        "ability_id": "risk_sentinel",
        "name": "Wary Obsidian Sentinel",
        "seed": "b31d7fa05c26e894713a0d6f5c82be47",
    },
    {
        "id": "ivory-archive",
        "ability_id": "filing_sleuth",
        "name": "Exact Ivory Archive",
        "seed": "2c8e46b1d90f37a5e6b24c17f085da39",
    },
    {
        "id": "cobalt-mapper",
        "ability_id": "pattern_mapper",
        "name": "Keen Cobalt Mapper",
        "seed": "9a45c0e37f18b62d4e07a935cb61d28f",
    },
    {
        "id": "copper-lens",
        "ability_id": "liquidity_reader",
        "name": "Steady Copper Lens",
        "seed": "47dbe2a91f6c3805b74de619af02c8d5",
    },
    {
        "id": "ruby-specter",
        "ability_id": "countervoice",
        "name": "Witty Ruby Specter",
        "seed": "e70b3d5a1c94f82603ad7e4b19c6f5a8",
    },
)
_VOICE_BY_ID = {str(voice["id"]): voice for voice in REPORT_VOICES}


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)


def _at(day: str, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(
        date.fromisoformat(day), clock_time(hour, minute), tzinfo=EASTERN
    ).astimezone(UTC)


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A text field is missing.")
    return " ".join(value.split())[:limit]


def _json_value(value: Any, fallback: Any) -> Any:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def voice_profile(voice: dict[str, str]) -> dict[str, Any]:
    ability = comment_avatar_ability(str(voice["ability_id"]))
    return {
        "id": str(voice["id"]),
        "name": str(voice["name"]),
        "ability_id": ability["id"],
        "ability": ability["label"],
        "ability_description": ability["description"],
        "focus": ability["prompt"],
        "avatar": comment_avatar_profile(
            str(voice["name"]), str(voice["seed"]), str(voice["ability_id"])
        ),
    }


def report_voices(report_id: str) -> list[dict[str, Any]]:

    offset = int(sha256(report_id.encode()).hexdigest()[:8], 16) % len(REPORT_VOICES)
    return [
        voice_profile(REPORT_VOICES[(offset + step) % len(REPORT_VOICES)])
        for step in range(min(VOICES_PER_REPORT, len(REPORT_VOICES)))
    ]


def queue_report_commentary(
    database: Any, report_id: str, report_day: str, report_type: str, at: datetime
) -> None:

    timestamp = _utc(at).isoformat()
    database.execute(
        """
        INSERT INTO market_report_commentary_jobs(
            report_id,report_day,report_type,status,created_at,updated_at
        ) VALUES(?,?,?,'queued',?,?) ON CONFLICT(report_id) DO NOTHING
        """,
        (report_id, report_day, report_type, timestamp, timestamp),
    )


def _report_context(database: Any, report_id: str) -> dict[str, Any] | None:
    row = database.execute(
        "SELECT * FROM market_session_reports WHERE id=?",
        (report_id,),
    ).fetchone()
    if not row:
        return None
    report = dict(row)
    context = {
        "id": str(report["id"]),
        "report_day": str(report["report_day"]),
        "report_type": str(report["report_type"]),
        "as_of": str(report["as_of"]),
        "headline": str(report["headline"]),
        "summary": str(report["summary"]),
        "metrics": _json_value(report.get("metrics_json"), {}),
        "leaders": _json_value(report.get("leaders_json"), []),
        "turns": _json_value(report.get("turns_json"), []),
    }
    attach_market_forecasts(database, [context])
    context["record"] = forecast_record(context["leaders"])
    return context


def _publishable_leader(leader: dict[str, Any]) -> dict[str, Any]:
    forecast = leader.get("eod_forecast") or {}
    row = {
        "ticker": leader.get("ticker"),
        "rank": leader.get("rank"),
        "score": leader.get("score"),
        "price": leader.get("price"),
        "change_pct": leader.get("change_pct"),
        "relative_volume": leader.get("relative_volume"),
        "trade_state": leader.get("trade_state"),
        "signals": list(leader.get("signals") or [])[:4],
        "risks": list(leader.get("risks") or [])[:4],
    }
    if leader.get("board_status"):
        row.update(
            board_status=leader.get("board_status"),
            close_price=leader.get("close_price"),
            close_rank=leader.get("close_rank"),
            close_change_pct=leader.get("close_change_pct"),
            session_return_pct=leader.get("session_return_pct"),
        )
    if forecast:
        row["eod_target"] = {
            "reference_price": forecast.get("reference_price"),
            "target_price": forecast.get("target_price"),
            "direction": forecast.get("direction"),
            "reason": forecast.get("reason"),
            "status": forecast.get("status"),
            "close_price": forecast.get("close_price"),
        }
    return row


def _commentary_request(
    report: dict[str, Any], voices: list[dict[str, Any]], current: datetime
) -> dict[str, Any]:
    pre_market = report["report_type"] == "pre_market"
    return {
        "actor": actor_snapshot(FLASH),
        "contract_version": CONTRACT_VERSION,
        "report_day": report["report_day"],
        "report_type": report["report_type"],
        "evidence_as_of": current.isoformat(),
        "task": (
            "Preview the session ahead and explain what the saved targets are betting on."
            if pre_market
            else "Review the finished session and explain how the saved targets scored."
        ),
        "tense": "before the session" if pre_market else "after the close",
        "report": {
            "headline": report["headline"],
            "summary": report["summary"],
            "as_of": report["as_of"],
            "metrics": report["metrics"],
            "target_record": report["record"],
            "leaders": [_publishable_leader(leader) for leader in report["leaders"]],
            "turns": report["turns"][:12],
        },
        "voices": [
            {
                "id": voice["id"],
                "name": voice["name"],
                "ability": voice["ability"],
                "description": voice["ability_description"],
                "focus": voice["focus"],
            }
            for voice in voices
        ],
    }


def _validated_commentary(
    request: dict[str, Any], result: Any
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(result, dict) or result.get("model") != request["actor"]["model"]:
        raise ValueError("The response must use the saved Flash model.")
    analysis = result.get("analysis")
    if not isinstance(analysis, dict):
        raise ValueError("The response must contain an analysis object.")
    points = analysis.get("points")
    if points is not None and not isinstance(points, list):
        raise ValueError("Analysis points must be a list.")
    saved_analysis = {
        "headline": _text(analysis.get("headline"), HEADLINE_MAX_CHARS),
        "narrative": _text(analysis.get("narrative"), NARRATIVE_MAX_CHARS),
        "points": [_text(point, POINT_MAX_CHARS) for point in (points or [])[:MAX_POINTS]],
        "model": request["actor"]["model"],
        "contract_version": request["contract_version"],
        "generated_at": request["evidence_as_of"],
    }

    values = result.get("comments")
    if not isinstance(values, list):
        raise ValueError("The response must contain a comment list.")
    requested = {str(voice["id"]): voice for voice in request["voices"]}
    bodies: dict[str, str] = {}
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("Each comment must be an object.")
        voice_id = raw.get("voice_id")
        if not isinstance(voice_id, str) or voice_id not in requested or voice_id in bodies:
            raise ValueError("Each requested voice must appear exactly once.")
        bodies[voice_id] = _text(raw.get("comment"), COMMENT_MAX_CHARS)
    if bodies.keys() != requested.keys():
        raise ValueError("Every requested voice needs a comment.")
    comments = [
        {"voice_id": str(voice["id"]), "body": bodies[str(voice["id"])]}
        for voice in request["voices"]
    ]
    return saved_analysis, comments


def _job_is_ready(database: Any, job: Any, current: datetime) -> bool:
    if str(job["report_type"]) == "pre_market":
        return True
    day = str(job["report_day"])
    if current >= _at(day, SETTLEMENT_WAIT_UNTIL.hour, SETTLEMENT_WAIT_UNTIL.minute):
        return True
    pending = database.execute(
        """
        SELECT 1 FROM market_report_forecasts
        WHERE report_day=? AND status='pending' LIMIT 1
        """,
        (day,),
    ).fetchone()
    return pending is None


def generate_report_commentary(
    generate: Callable[[dict[str, Any]], dict[str, Any]] | None,
    at: datetime | None = None,
) -> dict[str, int]:

    started = time.monotonic()
    current = _utc(at)
    timestamp = current.isoformat()
    if generate is None:
        return {"completed": 0, "failed": 0, "waiting": 0}
    with connection() as database:
        candidates = database.execute(
            """
            SELECT * FROM market_report_commentary_jobs WHERE attempts<?
              AND (status='queued' OR (status='running' AND lease_until<=?))
            ORDER BY created_at LIMIT 5
            """,
            (MAX_ATTEMPTS, timestamp),
        ).fetchall()
        waiting = 0
        job = None
        for candidate in candidates:
            if _job_is_ready(database, candidate, current):
                job = candidate
                break
            waiting += 1
        if job is None:
            return {"completed": 0, "failed": 0, "waiting": waiting}
        report = _report_context(database, str(job["report_id"]))
        if report is None:
            database.execute(
                """
                UPDATE market_report_commentary_jobs
                SET status='failed',last_error='MissingReport',updated_at=? WHERE report_id=?
                """,
                (timestamp, job["report_id"]),
            )
            return {"completed": 0, "failed": 1, "waiting": waiting}
        voices = report_voices(str(job["report_id"]))
        request = _commentary_request(report, voices, current)
        token = secrets.token_urlsafe(16)
        claimed = database.execute(
            """
            UPDATE market_report_commentary_jobs
            SET status='running',attempts=attempts+1,lease_token=?,lease_until=?,
                request_json=?,updated_at=?
            WHERE report_id=? AND attempts<?
              AND (status='queued' OR (status='running' AND lease_until<=?))
            """,
            (
                token,
                (current + timedelta(minutes=5)).isoformat(),
                json.dumps(request, separators=(",", ":")),
                timestamp,
                job["report_id"],
                MAX_ATTEMPTS,
                timestamp,
            ),
        ).rowcount
    if not claimed:
        return {"completed": 0, "failed": 0, "waiting": waiting}

    try:
        result = generate(request)
        analysis, comments = _validated_commentary(request, result)
    except Exception as exc:
        with connection() as database:
            database.execute(
                """
                UPDATE market_report_commentary_jobs
                SET status=CASE WHEN attempts>=? THEN 'failed' ELSE 'queued' END,
                    last_error=?,updated_at=?
                WHERE report_id=? AND status='running' AND lease_token=?
                """,
                (MAX_ATTEMPTS, type(exc).__name__, timestamp, job["report_id"], token),
            )
        return {"completed": 0, "failed": 1, "waiting": waiting}

    finished = (current + timedelta(seconds=max(0, time.monotonic() - started))).isoformat()
    with connection() as database:
        updated = database.execute(
            """
            UPDATE market_report_commentary_jobs
            SET status='complete',response_id=?,last_error=NULL,updated_at=?
            WHERE report_id=? AND status='running' AND lease_token=?
            """,
            (
                str(result.get("request_id") or "")[:160],
                finished,
                job["report_id"],
                token,
            ),
        ).rowcount
        if updated:
            database.execute(
                "UPDATE market_session_reports SET analysis_json=?,updated_at=? WHERE id=?",
                (
                    json.dumps(analysis, separators=(",", ":")),
                    finished,
                    job["report_id"],
                ),
            )
            for position, comment in enumerate(comments):
                database.execute(
                    """
                    INSERT INTO market_report_comments(
                        report_id,voice_id,position,body,model,created_at
                    ) VALUES(?,?,?,?,?,?) ON CONFLICT(report_id,voice_id) DO UPDATE SET
                        position=excluded.position,body=excluded.body,
                        model=excluded.model,created_at=excluded.created_at
                    """,
                    (
                        job["report_id"],
                        comment["voice_id"],
                        position,
                        comment["body"],
                        request["actor"]["model"],
                        finished,
                    ),
                )
    return {"completed": int(bool(updated)), "failed": 0, "waiting": waiting}


def attach_report_commentary(database: Any, reports: list[dict[str, Any]]) -> None:

    if not reports:
        return
    by_id = {str(report["id"]): report for report in reports}
    placeholders = ",".join("?" for _ in by_id)
    states = {
        str(row["report_id"]): str(row["status"])
        for row in database.execute(
            f"SELECT report_id,status FROM market_report_commentary_jobs "
            f"WHERE report_id IN ({placeholders})",
            list(by_id),
        ).fetchall()
    }
    for report in reports:
        report["commentary_state"] = states.get(str(report["id"]), "legacy")
        report["desk_comments"] = []
    rows = database.execute(
        f"SELECT * FROM market_report_comments WHERE report_id IN ({placeholders}) "
        f"ORDER BY report_id,position",
        list(by_id),
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        report = by_id.get(str(row["report_id"]))
        voice = _VOICE_BY_ID.get(str(row["voice_id"]))
        if report is None or voice is None:
            continue
        report["desk_comments"].append(
            {
                **voice_profile(voice),
                "body": str(row["body"]),
                "model": str(row["model"]),
                "created_at": str(row["created_at"]),
            }
        )
