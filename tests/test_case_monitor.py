import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.case_monitor import refresh_case_monitor
from runner_web.cases import create_case, get_case
from runner_web.db import connection, init_db


def _seed_user() -> None:
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("user", "user", "User", "active", datetime.now(UTC).isoformat()),
        )


def _case(ticker: str) -> dict[str, object]:
    return create_case(
        "user",
        ticker,
        thesis="Watching whether the move holds after the filing.",
        horizon_minutes=7200,
        reference_price=1.25,
        invalidation="Unknown — not supplied by the user.",
        risks=[],
        open_questions=[],
        confidence=None,
    )


def test_monitor_groups_news_with_its_primary_filing(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "case-monitor.db")
    init_db()
    _seed_user()
    case = _case("ONE")
    filed_at = datetime.now(UTC) + timedelta(seconds=1)
    collected_at = filed_at + timedelta(seconds=1)
    accession = "0001234567-26-000001"
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,
                filed_at,filing_url,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                accession,
                1234567,
                "ONE",
                "One Corp",
                "S-3",
                "offering",
                "risk",
                90,
                "Shelf registration could increase supply",
                filed_at.isoformat(),
                "https://www.sec.gov/Archives/one.htm",
                collected_at.isoformat(),
                collected_at.isoformat(),
            ),
        )
        database.execute(
            """
            INSERT INTO ingestion_runs(
                id,source,feed,locator,status,started_at,finished_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                "news-run",
                "yahoo",
                "news_search",
                "https://example.com/feed",
                "success",
                collected_at.isoformat(),
                collected_at.isoformat(),
            ),
        )
        database.execute(
            """
            INSERT INTO market_events(
                source,feed,event_id,version,ticker,event_type,event_at,published_at,
                status,source_url,payload_json,first_run_id,last_run_id,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "yahoo",
                "news_search",
                "article-one",
                "article-one",
                "ONE",
                "news_article",
                (filed_at + timedelta(minutes=5)).isoformat(),
                (filed_at + timedelta(minutes=5)).isoformat(),
                "published",
                "https://example.com/one-offering",
                json.dumps({"title": "One files a new registration offering"}),
                "news-run",
                "news-run",
                (collected_at + timedelta(seconds=1)).isoformat(),
                (collected_at + timedelta(seconds=1)).isoformat(),
            ),
        )

    first = refresh_case_monitor(collected_at + timedelta(seconds=2))
    second = refresh_case_monitor(collected_at + timedelta(seconds=3))

    assert first["updates"] == 1
    assert second["updates"] == 0
    with connection() as database:
        claims = database.execute("SELECT * FROM evidence_claims").fetchall()
        updates = database.execute(
            "SELECT * FROM thesis_case_updates WHERE case_id=? ORDER BY created_at",
            (case["id"],),
        ).fetchall()
    assert len(claims) == 1
    assert claims[0]["claim_key"] == f"sec:{accession}"
    assert claims[0]["source_count"] == 2
    assert len(updates) == 2
    assert updates[-1]["direction"] == "weakened"
    citations = json.loads(updates[-1]["citations_json"])
    assert citations[0]["url"].startswith("https://www.sec.gov/")
    assert citations[0]["source_count"] == 2
    current = get_case("user", str(case["public_id"]))
    assert current is not None
    assert current["latest_citations"][0]["source_count"] == 2


def test_monitor_keeps_a_hard_risk_veto_outside_the_model(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "case-veto.db")
    init_db()
    _seed_user()
    case = _case("RISK")
    captured_at = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,recent_relative_volume,breakout_pct,
                dollar_volume,quote_time,signals_json,risks_json,captured_at,
                setup_score,rug_score,rug_level,trade_state,state_reason,hard_veto
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "risk-snapshot",
                "RISK",
                80,
                "RUNNING",
                "regular",
                1.5,
                20,
                3,
                8,
                5,
                6,
                2,
                500_000,
                captured_at,
                "[]",
                '["Active halt"]',
                captured_at,
                80,
                95,
                "critical",
                "AVOID",
                "Active trading halt",
                1,
            ),
        )

    result = refresh_case_monitor()

    assert result["updates"] == 1
    with connection() as database:
        update = database.execute(
            """
            SELECT * FROM thesis_case_updates
            WHERE case_id=? AND kind='evidence_change'
            """,
            (case["id"],),
        ).fetchone()
    assert update is not None
    assert update["direction"] == "weakened"
    veto = json.loads(update["deterministic_veto_json"])
    assert veto["hard_veto"] is True
    assert veto["trade_state"] == "AVOID"
    assert update["model_provider"] is None
