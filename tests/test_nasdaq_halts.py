from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.nasdaq_halts import parse_trade_halts, refresh_trade_halts
from runner_web.source_workers import extended_us_session_is_open, trade_halts_enabled

HALT_RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:ndaq="http://www.nasdaqtrader.com/">
  <channel>
    <item>
      <title>Example Corp</title>
      <pubDate>Mon, 24 Aug 2026 18:32:10 GMT</pubDate>
      <ndaq:HaltDate>08/24/2026</ndaq:HaltDate>
      <ndaq:HaltTime>14:32:00</ndaq:HaltTime>
      <ndaq:IssueSymbol>PEN</ndaq:IssueSymbol>
      <ndaq:IssueName>Example Corp Common Stock</ndaq:IssueName>
      <ndaq:Market>NASDAQ</ndaq:Market>
      <ndaq:ReasonCode>T1</ndaq:ReasonCode>
      <ndaq:PauseThresholdPrice>1.25</ndaq:PauseThresholdPrice>
      <ndaq:ResumptionDate>08/24/2026</ndaq:ResumptionDate>
      <ndaq:ResumptionQuoteTime>14:57:00</ndaq:ResumptionQuoteTime>
      <ndaq:ResumptionTradeTime>15:02:00</ndaq:ResumptionTradeTime>
    </item>
  </channel>
</rss>
"""


def test_parse_trade_halts_preserves_source_times_and_state() -> None:
    events = parse_trade_halts(HALT_RSS)
    assert len(events) == 1
    event = events[0]
    assert event.event_id == "PEN:2026-08-24T18:32:00+00:00"
    assert event.ticker == "PEN"
    assert event.event_at == datetime(2026, 8, 24, 18, 32, tzinfo=UTC)
    assert event.published_at == datetime(2026, 8, 24, 18, 32, 10, tzinfo=UTC)
    assert event.status == "resume_announced"
    assert event.payload["reason_code"] == "T1"
    assert event.payload["quote_resume_at"] == "2026-08-24T18:57:00+00:00"
    assert event.payload["trade_resume_at"] == "2026-08-24T19:02:00+00:00"


def test_refresh_trade_halts_archives_and_deduplicates_the_feed(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "halts.db")
    init_db()

    def download(_url: str, _timeout: float) -> tuple[bytes, str]:
        return HALT_RSS, "application/rss+xml"

    first = refresh_trade_halts(download=download)
    second = refresh_trade_halts(download=download)
    with connection() as database:
        runs = database.execute(
            "SELECT COUNT(*) FROM ingestion_runs "
            "WHERE source='nasdaq_trader' AND feed='trade_halts'"
        ).fetchone()[0]
        documents = database.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
        events = database.execute("SELECT COUNT(*) FROM market_events").fetchone()[0]
        event = database.execute(
            "SELECT ticker,event_type,status,first_run_id,last_run_id FROM market_events"
        ).fetchone()
    assert first["events"] == 1
    assert second["events"] == 1
    assert runs == 2
    assert documents == 1
    assert events == 1
    assert tuple(event[:3]) == ("PEN", "trading_halt", "resume_announced")
    assert event["first_run_id"] == first["run_id"]
    assert event["last_run_id"] == second["run_id"]


def test_extended_session_schedule_uses_eastern_time() -> None:
    assert extended_us_session_is_open(datetime(2026, 8, 24, 8, tzinfo=UTC)) is True
    assert extended_us_session_is_open(datetime(2026, 8, 24, 23, 59, tzinfo=UTC)) is True
    assert extended_us_session_is_open(datetime(2026, 8, 25, 0, tzinfo=UTC)) is False
    assert extended_us_session_is_open(datetime(2026, 8, 23, 16, tzinfo=UTC)) is False


def test_trade_halt_worker_requires_an_explicit_opt_in(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("NASDAQ_TRADE_HALTS_ENABLED", raising=False)
    assert trade_halts_enabled() is False
    monkeypatch.setenv("NASDAQ_TRADE_HALTS_ENABLED", "true")
    assert trade_halts_enabled() is True
