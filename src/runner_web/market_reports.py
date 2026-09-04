from __future__ import annotations

import json
import secrets
import statistics
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from runner_web.db import connection
from runner_web.market_forecasts import attach_market_forecasts, queue_market_forecasts

EASTERN = ZoneInfo("America/New_York")
PRE_MARKET_START = time(4, 0)
PRE_MARKET_REPORT_AT = time(9, 0)
PRE_MARKET_CUTOFF = time(9, 15)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
POST_MARKET_REPORT_AT = time(16, 15)
POST_MARKET_CUTOFF = time(16, 20)

ReportType = Literal["pre_market", "post_market"]
REPORT_LABELS: dict[ReportType, str] = {
    "pre_market": "Pre-market briefing",
    "post_market": "Post-market recap",
}


def _as_eastern(moment: datetime | None) -> datetime:
    current = moment or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(EASTERN)


def _at(day: date, value: time) -> datetime:
    return datetime.combine(day, value, tzinfo=EASTERN)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def market_report_schedule(moment: datetime | None = None) -> dict[str, Any]:
    current = _as_eastern(moment)
    day = current.date()
    is_market_day = current.weekday() < 5
    pre_at = _at(day, PRE_MARKET_REPORT_AT)
    post_at = _at(day, POST_MARKET_REPORT_AT)
    due: list[ReportType] = []

    if is_market_day:
        if current >= pre_at:
            due.append("pre_market")
        if current >= post_at:
            due.append("post_market")

    if is_market_day and current < pre_at:
        next_type: ReportType = "pre_market"
        next_at = pre_at
    elif is_market_day and current < post_at:
        next_type = "post_market"
        next_at = post_at
    else:
        next_type = "pre_market"
        next_at = _at(_next_weekday(day), PRE_MARKET_REPORT_AT)

    return {
        "report_day": day.isoformat(),
        "is_market_day": is_market_day,
        "due": due,
        "next_report_type": next_type,
        "next_label": REPORT_LABELS[next_type],
        "next_at": _iso_utc(next_at),
        "seconds_to_next": max(0, int((next_at - current).total_seconds())),
        "schedule_note": "Weekdays · 9:00 ET and 4:15 ET",
    }


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _round(value: Any, digits: int = 2) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _scan_run(database: Any, start: datetime, end: datetime, *, latest: bool) -> Any:
    direction = "DESC" if latest else "ASC"
    return database.execute(
        f"""
        SELECT id,captured_at,candidate_rows FROM scan_runs
        WHERE captured_at>=? AND captured_at<=? AND candidate_rows>0
        ORDER BY captured_at {direction},id {direction} LIMIT 1
        """,
        (_iso_utc(start), _iso_utc(end)),
    ).fetchone()


def _snapshots(database: Any, scan_run_id: str) -> list[dict[str, Any]]:
    rows = database.execute(
        """
        SELECT id,ticker,score,setup_score,baseline_rank,stage,session,price,
               change_pct,momentum_15m_pct,relative_volume,recent_relative_volume,
               signals_json,risks_json,rug_score,trade_state,captured_at,quote_time
        FROM scan_snapshots WHERE scan_run_id=?
        ORDER BY score DESC,baseline_rank,ticker
        """,
        (scan_run_id,),
    ).fetchall()
    snapshots: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        snapshots.append(
            {
                "ticker": str(row["ticker"]),
                "rank": int(row.get("baseline_rank") or len(snapshots) + 1),
                "score": _round(row.get("score")) or 0.0,
                "setup_score": _round(row.get("setup_score")),
                "stage": str(row.get("stage") or ""),
                "session": str(row.get("session") or ""),
                "price": _round(row.get("price"), 4),
                "quote_time": row.get("quote_time"),
                "change_pct": _round(row.get("change_pct")),
                "momentum_15m_pct": _round(row.get("momentum_15m_pct")),
                "relative_volume": _round(row.get("relative_volume")),
                "recent_relative_volume": _round(row.get("recent_relative_volume")),
                "signals": [str(item) for item in _json_list(row.get("signals_json"))[:4]],
                "risks": [str(item) for item in _json_list(row.get("risks_json"))[:4]],
                "rug_score": _round(row.get("rug_score")),
                "trade_state": str(row.get("trade_state") or "UNKNOWN").upper(),
            }
        )
    return snapshots


def _market_breadth(rows: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [float(row["change_pct"]) for row in rows if row.get("change_pct") is not None]
    relative_volumes = [
        float(row["relative_volume"]) for row in rows if row.get("relative_volume") is not None
    ]
    return {
        "candidates": len(rows),
        "green": sum(value > 0 for value in changes),
        "red": sum(value < 0 for value in changes),
        "average_change_pct": round(statistics.fmean(changes), 2) if changes else None,
        "median_relative_volume": (
            round(statistics.median(relative_volumes), 2) if relative_volumes else None
        ),
        "high_risk": sum(row["trade_state"] in {"AVOID", "EXIT"} for row in rows),
    }


def _pre_market_payload(database: Any, day: date, current: datetime) -> dict[str, Any] | None:
    cutoff = min(current, _at(day, PRE_MARKET_CUTOFF))
    run = _scan_run(
        database,
        _at(day, PRE_MARKET_START),
        cutoff,
        latest=True,
    )
    if not run:
        return None
    rows = _snapshots(database, str(run["id"]))
    if not rows:
        return None
    metrics = _market_breadth(rows)
    leader = rows[0]
    captured_at = _as_eastern(datetime.fromisoformat(str(run["captured_at"])))
    summary = (
        f"{metrics['candidates']} names cleared the scanner by "
        f"{captured_at:%-I:%M %p} ET. "
        f"{metrics['green']} were green; {metrics['high_risk']} carried an avoid or exit state."
    )
    return {
        "source_scan_run_id": str(run["id"]),
        "comparison_scan_run_id": None,
        "as_of": str(run["captured_at"]),
        "headline": f"{leader['ticker']} leads the pre-market board",
        "summary": summary,
        "metrics": metrics,
        "leaders": rows[:8],
        "turns": [],
    }


def _post_market_turns(
    opening: list[dict[str, Any]],
    closing: list[dict[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    open_by_ticker = {row["ticker"]: row for row in opening}
    close_by_ticker = {row["ticker"]: row for row in closing}
    held = sorted(set(open_by_ticker) & set(close_by_ticker))
    joined = sorted(set(close_by_ticker) - set(open_by_ticker))
    dropped = sorted(set(open_by_ticker) - set(close_by_ticker))
    turns: list[dict[str, Any]] = []

    for ticker in held:
        first = open_by_ticker[ticker]
        last = close_by_ticker[ticker]
        first_price = first.get("price")
        last_price = last.get("price")
        checkpoint_return = None
        if first_price and last_price:
            checkpoint_return = round((float(last_price) / float(first_price) - 1) * 100, 2)
        turns.append(
            {
                "ticker": ticker,
                "status": "held",
                "open_rank": first["rank"],
                "close_rank": last["rank"],
                "rank_change": int(first["rank"]) - int(last["rank"]),
                "checkpoint_return_pct": checkpoint_return,
            }
        )
    for ticker in joined:
        last = close_by_ticker[ticker]
        turns.append(
            {
                "ticker": ticker,
                "status": "joined",
                "open_rank": None,
                "close_rank": last["rank"],
                "rank_change": None,
                "checkpoint_return_pct": None,
            }
        )
    for ticker in dropped:
        first = open_by_ticker[ticker]
        turns.append(
            {
                "ticker": ticker,
                "status": "dropped",
                "open_rank": first["rank"],
                "close_rank": None,
                "rank_change": None,
                "checkpoint_return_pct": None,
            }
        )

    turns.sort(
        key=lambda item: (
            {"joined": 0, "held": 1, "dropped": 2}[str(item["status"])],
            -int(item.get("rank_change") or 0),
            int(item.get("close_rank") or item.get("open_rank") or 1_000_000),
            str(item["ticker"]),
        )
    )
    return {"held": len(held), "joined": len(joined), "dropped": len(dropped)}, turns[:12]


def _post_market_payload(database: Any, day: date, current: datetime) -> dict[str, Any] | None:
    cutoff = min(current, _at(day, POST_MARKET_CUTOFF))
    opening_run = _scan_run(
        database,
        _at(day, REGULAR_OPEN),
        _at(day, REGULAR_CLOSE),
        latest=False,
    )
    closing_run = _scan_run(
        database,
        _at(day, time(15, 30)),
        cutoff,
        latest=True,
    )
    if not opening_run or not closing_run:
        return None
    opening = _snapshots(database, str(opening_run["id"]))
    closing = _snapshots(database, str(closing_run["id"]))
    if not opening or not closing:
        return None
    membership, turns = _post_market_turns(opening, closing)
    metrics = {**_market_breadth(closing), **membership}
    leader = closing[0]
    summary = (
        f"{metrics['candidates']} names finished on the close board. "
        f"{metrics['held']} held from the first regular-hours scan, "
        f"{metrics['joined']} joined, and {metrics['dropped']} dropped off."
    )
    return {
        "source_scan_run_id": str(closing_run["id"]),
        "comparison_scan_run_id": str(opening_run["id"]),
        "as_of": str(closing_run["captured_at"]),
        "headline": f"{leader['ticker']} finishes on top",
        "summary": summary,
        "metrics": metrics,
        "leaders": closing[:8],
        "turns": turns,
    }


def _report_record(row: Any) -> dict[str, Any] | None:
    if not row:
        return None
    report = dict(row)
    for key in ("metrics_json", "leaders_json", "turns_json"):
        try:
            report[key.removesuffix("_json")] = json.loads(str(report.get(key) or "[]"))
        except (TypeError, ValueError):
            report[key.removesuffix("_json")] = {} if key == "metrics_json" else []
        report.pop(key, None)
    report["label"] = REPORT_LABELS.get(report["report_type"], str(report["report_type"]))
    try:
        local_as_of = _as_eastern(datetime.fromisoformat(str(report["as_of"])))
        report["as_of_label"] = local_as_of.strftime("%-I:%M %p ET")
    except (TypeError, ValueError):
        report["as_of_label"] = "Time unavailable"
    report["metric_cards"] = _metric_cards(report)
    return report


def _metric_cards(report: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = report.get("metrics") or {}
    if report.get("report_type") == "post_market":
        return [
            {"label": "Close board", "value": metrics.get("candidates", 0)},
            {"label": "Held", "value": metrics.get("held", 0)},
            {"label": "Joined", "value": metrics.get("joined", 0)},
            {"label": "Dropped", "value": metrics.get("dropped", 0)},
        ]
    median_volume = metrics.get("median_relative_volume")
    return [
        {"label": "Watching", "value": metrics.get("candidates", 0)},
        {"label": "Green", "value": metrics.get("green", 0)},
        {
            "label": "Median RVOL",
            "value": f"{median_volume:.1f}×" if median_volume is not None else "—",
        },
        {"label": "High risk", "value": metrics.get("high_risk", 0)},
    ]


def market_report(report_day: str, report_type: ReportType) -> dict[str, Any] | None:
    with connection() as database:
        row = database.execute(
            "SELECT * FROM market_session_reports WHERE report_day=? AND report_type=?",
            (report_day, report_type),
        ).fetchone()
        report = _report_record(row)
        if report:
            attach_market_forecasts(database, [report])
    return report


def _create_report(
    report_type: ReportType,
    report_day: date,
    current: datetime,
) -> dict[str, Any] | None:
    with connection() as database:
        existing = database.execute(
            "SELECT * FROM market_session_reports WHERE report_day=? AND report_type=?",
            (report_day.isoformat(), report_type),
        ).fetchone()
        if existing:
            return _report_record(existing)
        payload = (
            _pre_market_payload(database, report_day, current)
            if report_type == "pre_market"
            else _post_market_payload(database, report_day, current)
        )
        if not payload:
            return None
        timestamp = _iso_utc(current)
        report_id = secrets.token_urlsafe(10)
        inserted = database.execute(
            """
            INSERT INTO market_session_reports(
                id,report_day,report_type,source_scan_run_id,comparison_scan_run_id,
                as_of,headline,summary,metrics_json,leaders_json,turns_json,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(report_day,report_type) DO NOTHING
            """,
            (
                report_id,
                report_day.isoformat(),
                report_type,
                payload["source_scan_run_id"],
                payload["comparison_scan_run_id"],
                payload["as_of"],
                payload["headline"],
                payload["summary"],
                json.dumps(payload["metrics"], separators=(",", ":")),
                json.dumps(payload["leaders"], separators=(",", ":")),
                json.dumps(payload["turns"], separators=(",", ":")),
                timestamp,
                timestamp,
            ),
        ).rowcount
        if inserted and report_type == "pre_market":
            queue_market_forecasts(
                database, report_id, report_day.isoformat(), payload["leaders"], current
            )
        row = database.execute(
            "SELECT * FROM market_session_reports WHERE report_day=? AND report_type=?",
            (report_day.isoformat(), report_type),
        ).fetchone()
    return _report_record(row)


def refresh_market_reports(moment: datetime | None = None) -> dict[str, Any]:
    current = _as_eastern(moment)
    schedule = market_report_schedule(current)
    results: list[dict[str, Any]] = []
    for report_type in schedule["due"]:
        existing = market_report(schedule["report_day"], report_type)
        if existing:
            results.append({"report_type": report_type, "status": "current", "id": existing["id"]})
            continue
        created = _create_report(report_type, current.date(), current)
        results.append(
            {
                "report_type": report_type,
                "status": "created" if created else "awaiting_scan",
                "id": created["id"] if created else None,
            }
        )
    return {"report_day": schedule["report_day"], "results": results, "schedule": schedule}


def market_reports_overview(
    moment: datetime | None = None,
    *,
    history_limit: int = 10,
) -> dict[str, Any]:
    schedule = market_report_schedule(moment)
    with connection() as database:
        rows = database.execute(
            """
            SELECT * FROM market_session_reports
            ORDER BY report_day DESC,
                     CASE report_type WHEN 'post_market' THEN 0 ELSE 1 END,
                     as_of DESC
            LIMIT ?
            """,
            (max(2, min(history_limit, 30)),),
        ).fetchall()
        reports = [report for row in rows if (report := _report_record(row))]
        attach_market_forecasts(database, reports)
    latest = {
        report_type: next(
            (report for report in reports if report["report_type"] == report_type),
            None,
        )
        for report_type in REPORT_LABELS
    }
    today = [report for report in reports if report["report_day"] == schedule["report_day"]]
    if "post_market" in schedule["due"] or not schedule["due"]:
        preferred: tuple[ReportType, ...] = ("post_market", "pre_market")
    else:
        preferred = ("pre_market", "post_market")
    featured = next(
        (report for kind in preferred for report in today if report["report_type"] == kind),
        None,
    ) or next((latest[kind] for kind in preferred if latest[kind]), None)
    return {
        "schedule": schedule,
        "latest": latest,
        "featured": featured,
        "reports": reports[:history_limit] if history_limit > 0 else [],
    }
