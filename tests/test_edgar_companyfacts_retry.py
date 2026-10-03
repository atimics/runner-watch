from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db, intelligence
from runner_web.db import connection, init_db

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def _scan(tickers: list[tuple[str, int]]) -> None:
    stamp = NOW.isoformat()
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_runs(
                id,mode,label,feature_schema_version,requested_symbols,liquid_symbols,
                scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                started_at,finished_at,captured_at
            ) VALUES('run','penny','Penny','test',1,1,1,1,'[]','[]',?,?,?)
            """,
            (stamp, stamp, stamp),
        )
        for ticker, cik in tickers:
            database.execute(
                "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) "
                "VALUES(?,?,?,?,?)",
                (cik, ticker, ticker, "Nasdaq", stamp),
            )
            database.execute(
                """
                INSERT INTO scan_snapshots(
                    id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                    momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                    dollar_volume,quote_time,signals_json,risks_json,captured_at,
                    scan_run_id,baseline_rank,trade_state
                ) VALUES(?,?,70,'BUILDING','pre',1,5,1,2,3,3,0.5,500000,?,'[]','[]',?,'run',1,
                    'watch')
                """,
                (f"snap-{ticker}", ticker, stamp, stamp),
            )


def test_a_company_with_no_facts_is_skipped_until_the_retry_window_passes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "facts.db")
    init_db()
    _scan([("AAA", 1001), ("BBB", 1002)])

    # Neither has facts, so the lower CIK sorts first.
    assert intelligence._companyfacts_candidate([]) == 1001
    intelligence._note_companyfacts_failure(1001, NOW)
    assert intelligence._companyfacts_candidate([]) == 1002
    intelligence._note_companyfacts_failure(1002, NOW)
    assert intelligence._companyfacts_candidate([]) is None


def test_failures_expire_and_the_list_is_capped(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "facts.db")
    init_db()
    old = NOW - timedelta(hours=intelligence.COMPANYFACTS_RETRY_HOURS + 1)
    intelligence._note_companyfacts_failure(1, old)
    intelligence._note_companyfacts_failure(2, NOW - timedelta(hours=1))
    assert set(intelligence._failed_companyfacts(NOW)) == {"2"}
    monkeypatch.setattr(intelligence, "COMPANYFACTS_FAILED_KEPT", 3)
    for cik in range(10, 16):
        intelligence._note_companyfacts_failure(cik, NOW)
    assert len(intelligence._failed_companyfacts(NOW)) == 3


def test_a_filing_event_still_refreshes_its_own_company(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "facts.db")
    init_db()
    intelligence._note_companyfacts_failure(7, NOW)
    assert intelligence._companyfacts_candidate([{"cik": 7}]) == 7
