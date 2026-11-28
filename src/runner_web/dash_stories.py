"""Matched filing comparisons and a private, ten-session editorial pilot."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from runner_watch.massive_data import MassiveDailyCache, massive_cache_path
from runner_web.dash_schedule import stored_instant
from runner_web.dash_sources import APPROVED_DOCUMENT
from runner_web.data_health import (
    recent_stock_closes,
    stock_session_open_for_day,
    stock_settlement_close,
)
from runner_web.db import connection
from runner_web.macro_feed import (
    BREADTH_SOURCE,
    macro_snapshot,
    material_macro_changes,
    render_macro_citations,
)

EASTERN = ZoneInfo("America/New_York")
BENCHMARK = "SPY"
PILOT_SESSIONS = 10
FILING_LOOKBACK = timedelta(days=14)


def _windows(filing_at: datetime):
    close = stock_settlement_close(filing_at)
    session = close.astimezone(EASTERN).date()
    opened = stock_session_open_for_day(session.isoformat())
    if opened is None or filing_at >= opened:
        return None
    prior, prior_close = recent_stock_closes(opened - timedelta(seconds=1), count=1)[0]
    return prior, session, opened, close, prior_close


def _return_pair(stock_start, stock_end, benchmark_start, benchmark_end):
    if any(
        value is None or value <= 0
        for value in (stock_start, stock_end, benchmark_start, benchmark_end)
    ):
        return None
    stock = 100 * (stock_end / stock_start - 1)
    benchmark = 100 * (benchmark_end / benchmark_start - 1)
    return {
        "stock_return_pct": round(stock, 2),
        "benchmark_return_pct": round(benchmark, 2),
        "stock_minus_benchmark_pp": round(stock - benchmark, 2),
    }


def _compare(row, window, bars, captures, halts, overlaps, current):
    accepted = stored_instant(row["filed_at"])
    if accepted is None or accepted == accepted.replace(hour=0, minute=0, second=0, microsecond=0):
        return {"status": "unavailable", "reason": "filing acceptance time unavailable"}
    if window is None:
        return {"status": "unavailable", "reason": "intraday filing requires aligned intraday bars"}
    prior, session, opened, close, prior_close = window
    ticker = row["ticker"]
    result = {
        "status": "pending",
        "accepted_at": accepted.isoformat(),
        "benchmark": BENCHMARK,
        "previous_session_date": prior.isoformat(),
        "session_date": session.isoformat(),
        "session_open_at": opened.isoformat(),
        "session_close_at": close.isoformat(),
        "nearby_filings_before_open": overlaps - 1,
        "basis": "Massive adjusted regular-session bars, matched to SPY; descriptive comparison",
        "source_url": BREADTH_SOURCE,
    }
    if any(start < close and (resume is None or resume > accepted) for start, resume in halts):
        return {
            **result,
            "status": "unavailable",
            "reason": "recorded halt overlaps the event window",
        }
    if close > current:
        return {**result, "reason": "regular session has yet to close"}
    if not (
        close <= captures.get(session, accepted) <= current
        and prior_close <= captures.get(prior, prior_close - timedelta(seconds=1)) <= current
    ):
        return {**result, "reason": "waiting for both finalized session bars"}
    stock, bench = bars.get(ticker, {}), bars.get(BENCHMARK, {})
    past, day = stock.get(prior, {}), stock.get(session, {})
    past_bench, day_bench = bench.get(prior, {}), bench.get(session, {})
    if any(not (bar.get("volume") or 0) > 0 for bar in (past, day, past_bench, day_bench)):
        return {**result, "reason": "waiting for traded bars in both sessions"}
    gap = _return_pair(
        past.get("close"), day.get("open"), past_bench.get("close"), day_bench.get("open")
    )
    session_return = _return_pair(
        day.get("open"), day.get("close"), day_bench.get("open"), day_bench.get("close")
    )
    if gap is None or session_return is None:
        return {**result, "reason": "one or more matched prices unavailable"}
    return {
        **result,
        "status": "available",
        "prices_collected_at": max(captures[session], captures[prior]).isoformat(),
        "previous_close_to_regular_open": gap,
        "regular_open_to_close": session_return,
    }


def refresh_filing_windows(*, at: datetime | None = None) -> int:
    current = at or datetime.now(UTC)
    cache_path = massive_cache_path()
    if not cache_path.is_file():
        return 0
    cutoff = (current - FILING_LOOKBACK).isoformat()
    with connection() as database:
        rows = database.execute(
            "SELECT accession,ticker,filed_at FROM sec_filings WHERE filed_at BETWEEN ? AND ? "
            "AND form IN ('8-K','8-K/A','10-K','10-K/A','10-Q','10-Q/A','6-K','6-K/A') "
            "ORDER BY filed_at DESC,accession DESC LIMIT 600",
            (cutoff, current.isoformat()),
        ).fetchall()
        halt_rows = database.execute(
            "SELECT ticker,event_at,payload_json FROM public_market_events "
            "WHERE event_type='trading_halt' AND event_at BETWEEN ? AND ?",
            ((current - timedelta(days=30)).isoformat(), current.isoformat()),
        ).fetchall()
    if not rows:
        return 0
    windows = {}
    instances = Counter()
    for row in rows:
        accepted = stored_instant(row["filed_at"])
        windows[row["accession"]] = window = _windows(accepted) if accepted else None
        if window and accepted.hour + accepted.minute + accepted.second:
            instances[(row["ticker"], window[1])] += 1
    halts = {}
    for row in halt_rows:
        if moment := stored_instant(row["event_at"]):
            end = stored_instant(json.loads(row["payload_json"] or "{}").get("trade_resume_at"))
            halts.setdefault(row["ticker"], []).append((moment, end))
    evergreen = (current - timedelta(days=21)).astimezone(EASTERN).date()
    with MassiveDailyCache(cache_path) as cache:
        history = cache.bars_for({BENCHMARK, *(row["ticker"] for row in rows)}, since=evergreen)
        captures = cache.capture_times(evergreen)
    bars = {ticker: dict(prices) for ticker, prices in history.items()}
    records = []
    for row in rows:
        window = windows[row["accession"]]
        comparison = _compare(
            row,
            window,
            bars,
            captures,
            halts.get(row["ticker"], ()),
            instances[(row["ticker"], window[1])] if window else 0,
            current,
        )
        records.append((row["accession"], row["filed_at"], json.dumps(comparison)))
    with connection() as database:
        database.executemany(
            "INSERT INTO dash_filing_windows(accession,accepted_at,comparison_json) VALUES(?,?,?) "
            "ON CONFLICT(accession) DO UPDATE SET accepted_at=excluded.accepted_at,"
            "comparison_json=excluded.comparison_json "
            "WHERE dash_filing_windows.comparison_json<>excluded.comparison_json",
            records,
        )
        database.execute("DELETE FROM dash_filing_windows WHERE accepted_at<?", (cutoff,))
    return len(records)


def filing_windows(accessions: list[str]) -> dict[str, dict]:
    if not accessions:
        return {}
    with connection() as database:
        rows = database.execute(
            "SELECT accession,comparison_json FROM dash_filing_windows "
            f"WHERE accession IN ({','.join('?' for _ in accessions)})",
            tuple(accessions),
        ).fetchall()
    return {row["accession"]: json.loads(row["comparison_json"]) for row in rows}


def _sports_draft(database, observed):
    from runner_web.dash import DASH_SPORTS_PREDICTION

    rows = database.execute(
        f"""
        SELECT e.id AS event_id,e.league,p.id,p.home_probability,p.selection,p.signal,
               p.decision_receipt_json
        FROM sports_events e JOIN sports_predictions p ON p.id=({DASH_SPORTS_PREDICTION})
        WHERE e.status='pre' AND e.start_time>? AND e.start_time<=?
          AND e.season_type NOT IN ('preseason','pre-season')
          AND p.decision_receipt_json IS NOT NULL
        ORDER BY e.start_time,e.id LIMIT 12
        """,
        (observed.isoformat(), observed.isoformat(), (observed + timedelta(days=1)).isoformat()),
    ).fetchall()
    games = []
    for row in rows:
        game = dict(row)
        receipt = json.loads(game.pop("decision_receipt_json"))
        if reference := receipt.get("reference"):
            game["market_home_probability"] = reference["home_probability"]
            game["offer"] = receipt["offer"]
            games.append(game)
    if len(games) < 4:
        return None
    return {
        "question": "How did Dash's pregame forecasts score against the same-time market?",
        "finding": f"{len(games)} paired decisions, with passes retained for forecast scoring.",
        "next_check": "Score these same games after settlement and count any missing prices.",
        "observed_at": observed.isoformat(),
        "games": games,
    }


def _pilot_draft(database, session, close, now):
    previous_close = recent_stock_closes(close - timedelta(seconds=1), count=1)[0][1]
    cut = min(now, stock_settlement_close(close))
    kinds = {row[0] for row in database.execute("SELECT DISTINCT story_kind FROM dash_story_pilot")}
    filing = database.execute(
        f"""
        SELECT f.accession,f.ticker,f.form,f.filed_at,p.headline,p.source_url,w.comparison_json
        FROM sec_filing_previews p JOIN sec_filings f ON f.accession=p.accession
        JOIN source_documents d ON d.source='sec' AND d.source_url=p.source_url
                               AND d.content_hash=p.content_hash
        LEFT JOIN dash_filing_windows w ON w.accession=f.accession
        WHERE p.updated_at BETWEEN ? AND ? AND f.filed_at BETWEEN ? AND ?
          AND {APPROVED_DOCUMENT}
          AND NOT EXISTS(SELECT 1 FROM dash_story_pilot s
                         WHERE s.story_key='filing:' || f.accession)
        ORDER BY CASE WHEN f.form LIKE ? THEN 0 ELSE 1 END,f.score DESC,p.updated_at DESC
        LIMIT 1
        """,
        (
            previous_close.isoformat(),
            cut.isoformat(),
            (close - FILING_LOOKBACK).isoformat(),
            cut.isoformat(),
            "%/A",
        ),
    ).fetchone()
    filing_draft = None
    if filing:
        filing_draft = {
            "question": (
                f"What changed in {filing['ticker']}'s {filing['form']}, "
                "and how did it trade against SPY?"
            ),
            "finding": filing["headline"],
            "next_check": "Check the first regular trading window and any exchange update.",
            "accession": filing["accession"],
            "ticker": filing["ticker"],
            "accepted_at": filing["filed_at"],
            "source_url": filing["source_url"],
            "comparison": json.loads(filing["comparison_json"] or "{}"),
        }
        if "filing" not in kinds:
            return "filing", filing_draft
    macro = macro_snapshot(cut, breadth_session=session)
    breadth = macro["breadth"]
    breadth_draft = None
    if breadth.get("session_date") == session.isoformat():
        text, _ = render_macro_citations("[macro:breadth]", {"macro": macro})
        breadth_draft = {
            "question": "How broad was the move at the close?",
            "finding": text,
            "scanner_cohort": breadth.get("scanner_cohort"),
            "next_check": "Compare participation after the next close using the same basis.",
        }
        if "breadth" not in kinds:
            return "breadth", breadth_draft
    if "sports" not in kinds and (sports := _sports_draft(database, now)):
        return "sports", sports
    for update in material_macro_changes(macro, previous_close):
        if update["section"] == "breadth":
            continue
        text, _ = render_macro_citations(update["citation"], {"macro": macro})
        return "macro", {
            "question": "Which published U.S. macro reading changed enough to revisit?",
            "finding": text,
            "section": update["section"],
            "reading": update["reading"],
            "citation": update["citation"],
            "change_bps": update.get("change_bps"),
            "next_check": "Look for a new observation or revision at the next review.",
        }
    if filing_draft:
        return "filing", filing_draft
    if breadth_draft:
        return "breadth", breadth_draft
    return "quiet", {"reason": "No qualifying readable filing or closing breadth was available."}


def _pilot_review(row, now):
    draft = json.loads(row["draft_json"])
    if row["story_kind"] == "filing":
        item = filing_windows([draft["accession"]]).get(draft["accession"], {})
        if item.get("status") == "available":
            return {"status": "resolved", "comparison": item}
        return {"status": "unresolved", "reason": item.get("reason", "matched prices unavailable")}
    if row["story_kind"] == "sports":
        from runner_web.dash import _forecast_scores, _game_outcome
        from runner_web.sports import _american_profit

        games = draft["games"]
        with connection() as database:
            finals = {
                item["id"]: dict(item)
                for item in database.execute(
                    "SELECT id,completed,home_score,away_score FROM sports_events "
                    f"WHERE id IN ({','.join('?' for _ in games)})",
                    tuple(game["event_id"] for game in games),
                ).fetchall()
            }
        forecasts = []
        pending = ties = priced = 0
        units = 0.0
        for game in games:
            result = _game_outcome(finals.get(game["event_id"], {}), "home")
            if result is None:
                pending += 1
                continue
            if result == "push":
                ties += 1
            else:
                forecasts.append(
                    (
                        game["home_probability"],
                        game["market_home_probability"],
                        int(result == "win"),
                    )
                )
            if offer := game.get("offer"):
                priced += 1
                if result != "push":
                    won = (result == "win") == (game["selection"] == "home")
                    units += _american_profit(offer["odds"]) if won else -1.0
        return {
            "status": "unresolved" if pending else "resolved",
            "pending": pending,
            "ties": ties,
            "forecast_quality": _forecast_scores(forecasts),
            "passed": sum(game["selection"] == "pass" for game in games),
            "priced": priced,
            "flat_stake_roi_pct": round(100 * units / priced, 1) if priced else None,
        }
    if row["story_kind"] == "macro":
        section = draft["section"]
        macro = macro_snapshot(now, section=section)
        reading = macro[section].get(draft["reading"], {})
        if reading.get("status") == "available":
            text, _ = render_macro_citations(draft["citation"], {"macro": macro})
            return {"status": "resolved", "changed": text != draft["finding"], "finding": text}
        return {"status": "unresolved", "reason": "comparable macro reading unavailable"}
    next_close = stored_instant(row["review_at"])
    target = next_close.astimezone(EASTERN).date() if next_close else None
    macro = macro_snapshot(now, section="breadth", breadth_session=target)
    breadth = macro["breadth"]
    if (
        next_close
        and breadth.get("session_date") == next_close.astimezone(EASTERN).date().isoformat()
    ):
        text, _ = render_macro_citations("[macro:breadth]", {"macro": macro})
        return {
            "status": "resolved",
            "finding": text,
            "previous_reading": breadth.get("previous_reading"),
        }
    return {"status": "unresolved", "reason": "next completed-session breadth has yet to arrive"}


def refresh_story_pilot(*, at: datetime | None = None) -> int:
    """Generate at most one private work sample per completed session."""
    current = at or datetime.now(UTC)
    stamp = current.isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES('dash_story_pilot_start',?,?) "
            "ON CONFLICT DO NOTHING",
            (stamp, stamp),
        )
        start = database.execute(
            "SELECT value FROM worker_state WHERE key='dash_story_pilot_start'"
        ).fetchone()["value"]
        existing = database.execute(
            "SELECT * FROM dash_story_pilot ORDER BY session_date"
        ).fetchall()
        due = [
            row
            for row in existing
            if row["review_at"] <= stamp
            and row["review_at"] >= (current - timedelta(days=7)).isoformat()
            and row["story_kind"] != "quiet"
            and json.loads(row["review_json"] or "{}").get("status") != "resolved"
        ]
    for row in due:
        result = _pilot_review(row, current)
        with connection() as database:
            database.execute(
                "UPDATE dash_story_pilot SET reviewed_at=COALESCE(reviewed_at,?),review_json=? "
                "WHERE session_date=?",
                (stamp, json.dumps(result), row["session_date"]),
            )
    if len(existing) >= PILOT_SESSIONS:
        return 0
    seen = {row["session_date"] for row in existing}
    created = 0
    for session, close in reversed(recent_stock_closes(current, count=PILOT_SESSIONS)):
        if (
            close.isoformat() < start
            or session.isoformat() in seen
            or close + timedelta(minutes=45) > current
        ):
            continue
        with connection() as database:
            kind, draft = _pilot_draft(database, session, close, current)
            if kind == "quiet" and current < stock_settlement_close(close):
                continue
            review_at = stock_settlement_close(close).isoformat()
            inserted = database.execute(
                "INSERT INTO dash_story_pilot"
                "(session_date,story_kind,draft_json,created_at,review_at,story_key) "
                "SELECT ?,?,?,?,?,? WHERE (SELECT COUNT(*) FROM dash_story_pilot)<? "
                "ON CONFLICT DO NOTHING RETURNING session_date",
                (
                    session.isoformat(),
                    kind,
                    json.dumps(draft),
                    stamp,
                    review_at,
                    "filing:" + draft["accession"] if kind == "filing" else None,
                    PILOT_SESSIONS,
                ),
            ).fetchone()
        created += int(bool(inserted))
    return created


def story_pilot_status(*, at: datetime | None = None) -> dict:
    current = at or datetime.now(UTC)
    with connection() as database:
        rows = [
            dict(row)
            for row in database.execute(
                "SELECT * FROM dash_story_pilot ORDER BY session_date DESC"
            ).fetchall()
        ]
    due = [
        row
        for row in rows
        if row["story_kind"] != "quiet" and row["review_at"] <= current.isoformat()
    ]
    for row in rows:
        row["draft"] = json.loads(row.pop("draft_json"))
        row["review"] = json.loads(row.pop("review_json") or "{}")
        row["feedback"] = json.loads(row.pop("feedback_json") or "{}")
    return {
        "mode": "private_drafts",
        "target_sessions": PILOT_SESSIONS,
        "recorded_sessions": len(rows),
        "reviews_due": len(due),
        "reviews_attempted": sum(bool(row["reviewed_at"]) for row in due),
        "reviewed_within_one_hour": sum(
            bool(
                (reviewed := stored_instant(row["reviewed_at"]))
                and reviewed <= stored_instant(row["review_at"]) + timedelta(hours=1)
            )
            for row in due
        ),
        "resolved": sum(row["review"].get("status") == "resolved" for row in due),
        "rated_useful": sum(row["feedback"].get("rating") == "useful" for row in rows),
        "sessions": rows,
    }


def save_pilot_feedback(session: str, rating: str, question: str) -> bool:
    feedback = {"rating": rating, "question_to_follow": question.strip()[:240]}
    with connection() as database:
        return (
            database.execute(
                "UPDATE dash_story_pilot SET feedback_json=? "
                "WHERE session_date=? RETURNING session_date",
                (json.dumps(feedback), session),
            ).fetchone()
            is not None
        )
