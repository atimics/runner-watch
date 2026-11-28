from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from runner_watch.massive_data import MassiveDailyCache
from runner_web import dash_jobs, dash_stories, db, macro_feed
from runner_web.db import connection
from runner_web.telegram import TelegramConfig
from runner_web.telegram_outbox import deliver_outbox

FRIDAY = datetime(2026, 11, 27, 20, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "dash.sqlite3")
    monkeypatch.setenv("MASSIVE_CACHE_PATH", str(tmp_path / "massive.sqlite3"))
    monkeypatch.setenv("TELEGRAM_CHANNEL_INTERVAL_SECONDS", "0")
    db.init_db()


def test_filing_windows_align_regular_open_to_spy_across_thanksgiving():
    with MassiveDailyCache(dash_stories.massive_cache_path()) as cache:
        cache.store_date(
            date(2026, 11, 25),
            [
                {"T": "AREC", "o": 10, "c": 10, "v": 100},
                {"T": "SPY", "o": 500, "c": 500, "v": 100},
            ],
        )
        cache.store_date(
            date(2026, 11, 27),
            [
                {"T": "AREC", "o": 8, "c": 9, "v": 100},
                {"T": "SPY", "o": 505, "c": 510, "v": 100},
            ],
        )
    with sqlite3.connect(dash_stories.massive_cache_path()) as cache_db:
        cache_db.execute("UPDATE fetched_dates SET fetched_at=?", (FRIDAY.isoformat(),))
    with connection() as database:
        database.executemany(
            "INSERT INTO sec_filings(accession,cik,ticker,company,form,kind,sentiment,score,"
            "title,filed_at,filing_url,created_at,updated_at) "
            "VALUES(?,1,'AREC','Test','8-K','filing','neutral',10,'Test',?,'test',?,?)",
            [
                (accession, stamp, stamp, stamp)
                for accession, stamp in [
                    ("overnight", "2026-11-25T22:15:00+00:00"),
                    ("overlap", "2026-11-27T13:15:00+00:00"),
                    ("intraday", "2026-11-27T15:15:00+00:00"),
                    ("dateonly", "2026-11-27T00:00:00+00:00"),
                ]
            ],
        )
    assert dash_stories.refresh_filing_windows(at=FRIDAY) == 4
    saved = dash_stories.filing_windows(["overnight", "intraday", "dateonly"])
    comparison = saved["overnight"]
    assert comparison["status"] == "available"
    assert comparison["session_open_at"] == "2026-11-27T14:30:00+00:00"
    assert comparison["session_close_at"] == "2026-11-27T18:00:00+00:00"
    assert comparison["nearby_filings_before_open"] == 1
    assert comparison["previous_close_to_regular_open"]["stock_minus_benchmark_pp"] == -21
    assert comparison["regular_open_to_close"]["stock_return_pct"] == 12.5
    assert "intraday" in saved["intraday"]["reason"]
    assert "acceptance" in saved["dateonly"]["reason"]


def test_macro_reference_keeps_the_actual_period_and_basis():
    reading = {
        "citation": "[macro:inflation.headline_year_over_year]",
        "status": "partial",
        "source_url": "https://fred.stlouisfed.org/series/CPIAUCNS",
        "observation_month": "2026-10",
        "change_pct": None,
        "last_comparable": {"observation_month": "2026-09", "change_pct": 3.74},
    }
    evidence = {"macro:inflation": {"inflation": {"headline_year_over_year": reading}}}
    text, missing = macro_feed.render_macro_citations(reading["citation"], evidence)
    assert missing == []
    assert "unavailable for 2026-10, latest comparable +3.74% in 2026-09" in text
    assert "CPIAUCNS" in text
    _, missing = macro_feed.render_macro_citations("[macro:rates.bad_key]", evidence)
    assert missing == ["rates.bad_key"]


def test_story_version_threads_once_and_deduplicates_new_wording():
    config = TelegramConfig("fixture", "-100")
    scope = "chat:-100:topic:0"
    sent = []

    def capture(_config, text, **thread):
        sent.append((text, thread.get("reply_to_message_id")))
        return 100 + len(sent)

    def finish(key, version, text, at):
        claimed = dash_jobs.claim(key, scope, "desk", at)
        result = {
            "action": "reply",
            "text": text,
            "story": {"node": "ticker:AREC", "version": version},
        }
        dash_jobs.finish(key, claimed["token"], scope, result, {}, at, chat_id=-100)
        return result

    finish("story-one", "version-one", "The filing is still queued.", FRIDAY)
    kinds = ("dash_research",)
    assert (
        deliver_outbox(config, capture, at=FRIDAY, kinds=kinds, clock=lambda: FRIDAY)["sent"] == 1
    )
    later = FRIDAY + timedelta(hours=1)
    finish("story-two", "version-two", "The filing is readable.", later)
    duplicate = finish("story-duplicate", "version-two", "We now have the filing.", later)
    assert duplicate["delivery"]["status"] == "duplicate"
    assert deliver_outbox(config, capture, at=later, kinds=kinds, clock=lambda: later)["sent"] == 1
    assert deliver_outbox(config, capture, at=later, kinds=kinds)["sent"] == 0
    assert sent == [("The filing is still queued.", None), ("The filing is readable.", 101)]


def test_private_pilot_records_one_session_and_keeps_reviews_out_of_the_outbox(monkeypatch):
    def draft(database, session, close, now):
        return "breadth", {
            "finding": "Dated participation",
            "next_check": "Compare the next close.",
        }

    monkeypatch.setattr(dash_stories, "_pilot_draft", draft)
    monkeypatch.setattr(dash_stories, "_pilot_review", lambda row, now: {"status": "resolved"})
    start = FRIDAY.replace(hour=15)
    assert dash_stories.refresh_story_pilot(at=start) == 0
    assert dash_stories.refresh_story_pilot(at=FRIDAY) == 1
    assert dash_stories.refresh_story_pilot(at=FRIDAY) == 0
    assert dash_stories.save_pilot_feedback("2026-11-27", "useful", "What changed in the scanner?")
    monday_close = datetime(2026, 11, 30, 21, tzinfo=UTC)
    assert dash_stories.refresh_story_pilot(at=monday_close) == 0
    report = dash_stories.story_pilot_status(at=monday_close)
    assert report["recorded_sessions"] == report["reviews_due"] == report["resolved"] == 1
    assert report["rated_useful"] == report["reviewed_within_one_hour"] == 1
    assert report["sessions"][0]["feedback"]["question_to_follow"] == "What changed in the scanner?"
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0] == 0


def test_arec_is_first_private_notebook_entry_and_keeps_the_two_checks_separate(monkeypatch):
    from runner_web import dash_research, quotes

    saturday = datetime(2026, 11, 28, 2, tzinfo=UTC)
    opened = datetime(2026, 11, 30, 14, 30, tzinfo=UTC)
    first_read = opened + timedelta(minutes=5)

    assert dash_stories.refresh_story_pilot(at=saturday) == 1
    assert dash_stories.refresh_story_pilot(at=saturday + timedelta(hours=1)) == 0
    status = dash_stories.story_pilot_status(at=saturday)
    assert status["sessions"][0]["draft"]["ticker"] == "AREC"
    assert status["sessions"][0]["review_at"] == opened.isoformat()
    (note,) = dash_research.notebook(dash_stories.PILOT_NOTEBOOK_SCOPE, at=saturday)
    assert note["node"] == "ticker:AREC"
    assert note["review_outcome"] == "planned"
    assert dash_research.notebook("chat:-100:topic:0", at=saturday) == []

    monkeypatch.setattr(
        quotes,
        "market_mark",
        lambda ticker, at: {
            "price": 0.8,
            "source": "quote",
            "price_source": "yahoo",
            "observed_at": first_read.isoformat(),
        },
    )
    assert dash_stories.refresh_story_pilot(at=first_read) == 0
    review = dash_stories.story_pilot_status(at=first_read)["sessions"][0]["review"]
    assert review["regular_session_print"]["status"] == "observed"
    assert review["nasdaq_timetable"]["status"] == "unconfirmed"
    assert review["status"] == "unresolved"
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0] == 0
