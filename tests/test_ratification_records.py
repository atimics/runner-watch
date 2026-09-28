from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

import ratitrust
from runner_web import db, ratification_records
from runner_web.ratification_records import history, record_results, rules_digest, safely_record

AT = datetime(2026, 9, 28, 12, tzinfo=UTC)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "records.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()


def result(ratified=True, detail="626 hours", age=True):
    return {
        "ratified": ratified,
        "met": 9 if ratified else 8,
        "total": 9,
        "standards": [
            {"key": "age", "met": age, "applies": True, "detail": detail},
            {"key": "pool", "met": ratified, "applies": True, "detail": "$31,096 real liquidity"},
        ],
    }


def test_the_recorded_digest_is_the_one_published_for_the_rules_in_force():
    from runner_web import trust

    current = trust.rules_record()["current"]
    assert (ratitrust.__version__, rules_digest()) == (current["version"], current["digest"])


def test_a_result_is_recorded_when_its_state_changes_not_when_a_detail_moves(database):
    facts = {"row": {"real_liquidity_usd": 31096.0}, "holder_count": 1000}
    with db.connection() as database_:
        assert record_results(database_, "memecoin", {"MINT": (result(), facts)}, at=AT) == 1
        later = AT + timedelta(hours=1)
        moved = {"MINT": (result(detail="627 hours"), facts)}
        assert record_results(database_, "memecoin", moved, at=later) == 0
        lost = {"MINT": (result(ratified=False), facts)}
        assert record_results(database_, "memecoin", lost, at=later) == 1
        records = history(database_, "memecoin", "MINT")

    assert [record["ratified"] for record in records] == [False, True]
    first = records[-1]
    assert (
        first["rules_version"] == ratitrust.__version__ and first["rules_digest"] == rules_digest()
    )
    assert first["facts"] == facts and first["result"]["standards"][0]["detail"] == "626 hours"


def test_stock_facts_with_dates_are_kept(database):
    facts = {"halted_on": date(2026, 9, 20), "issuer": {"periodic_form": "10-Q"}}
    safely_record("stock", {"KLXE": (result(), facts)}, at=AT)

    with db.connection() as database_:
        assert history(database_, "stock", "KLXE")[0]["facts"]["halted_on"] == "2026-09-20"


def test_a_failed_write_is_logged_and_never_raised(monkeypatch, caplog):
    def broken(*_a, **_k):
        raise RuntimeError("database is down")

    monkeypatch.setattr(ratification_records, "record_results", broken)

    safely_record("stock", {"KLXE": (result(), {})}, at=AT)

    assert "Ratification records failed for stock" in caplog.text


def test_a_ratified_page_read_leaves_a_record(database, monkeypatch):
    from runner_web import main, stock_ratify

    monkeypatch.setattr(
        stock_ratify, "stock_ratifications", lambda _db, rows, at, facts: _fake(rows, facts)
    )

    main._with_stock_ratification([{"ticker": "KLXE"}])

    with db.connection() as database_:
        record = history(database_, "stock", "KLXE")[0]
    assert record["ratified"] is True and record["facts"] == {"exchange": "Nasdaq"}
    assert json.dumps(record["result"])


def _fake(rows, facts):
    facts["KLXE"] = {"exchange": "Nasdaq"}
    return {"KLXE": result()}


def test_a_recorded_result_reproduces_under_the_rules_that_recorded_it(database):
    from datetime import date

    from ratitrust import stock
    from ratitrust.reproduce import reproduce

    facts = {
        "exchange": "Nasdaq",
        "issuer": {"issuer_data_available": True, "periodic_filed_at": "2026-04-28"},
        "halted_on": None,
        "delisting_on": date(2026, 8, 1),
        "today": AT.date(),
        "filed": {"filed_at": "2026-04-28", "form": "20-F", "forms": {"20-F", "6-K"}},
        "delistings_read": True,
    }
    safely_record("stock", {"SHMD": (stock.standards(**facts), facts)}, at=AT)

    with db.connection() as database_:
        record = history(database_, "stock", "SHMD")[0]
    assert record["facts"]["filed"]["forms"] == ["20-F", "6-K"]
    assert reproduce("stock", record["facts"]) == record["result"]


def test_an_asset_page_shows_its_state_since_it_was_recorded_and_earlier_changes():
    from runner_web.market_screens import detail
    from tests.test_market_screens import render

    history_ = [
        {
            "id": 7,
            "ratified": True,
            "met": 5,
            "total": 5,
            "unchecked": 0,
            "rules_version": "2.1.0",
            "rules_digest": "78312512b3ca22a2",
            "recorded_label": "Sep 28, 09:10 UTC",
            "url": "/api/ratification/stock/GAU/7.json",
        },
        {
            "id": 3,
            "ratified": False,
            "met": 4,
            "total": 5,
            "unchecked": 1,
            "rules_version": "2.0.0",
            "rules_digest": "cd0357851f936c36",
            "recorded_label": "Sep 28, 07:33 UTC",
            "url": "/api/ratification/stock/GAU/3.json",
        },
    ]
    stock = {
        "ticker": "GAU",
        "price": 2.0,
        "ratification": result(),
        "ratification_history": history_,
    }

    page = render(detail("stocks", {"current": stock, "ticker": "GAU"}))

    assert "Ratified since Sep 28, 09:10 UTC" in page
    assert 'RATi Rules</a> 2.1.0 <code title="78312512b3ca22a2">78312512</code>' in page
    assert 'href="/api/ratification/stock/GAU/7.json" download>Record</a>' in page
    assert "Sep 28, 07:33 UTC · 4 of 5 met · 2.0.0" in page


def test_records_are_served_with_what_reproduces_them(database):
    from fastapi.testclient import TestClient

    from ratitrust import stock
    from runner_web import main

    facts = {
        "exchange": "NYSE",
        "issuer": {"issuer_data_available": True, "periodic_filed_at": "2026-08-20"},
        "halted_on": None,
        "delisting_on": None,
        "today": AT.date(),
        "filed": None,
        "delistings_read": True,
    }
    safely_record("stock", {"GAU": (stock.standards(**facts), facts)}, at=AT)
    main.RATE_LIMITS.clear()
    client = TestClient(main.app, base_url=main.RUNNERS_ORIGIN)

    listing = client.get("/api/ratification/stock/gau").json()["records"]
    one = client.get(listing[0]["url"]).json()

    from ratitrust.reproduce import reproduce

    assert one["subject"] == "GAU" and one["rules_version"] == ratitrust.__version__
    assert reproduce("stock", one["facts"]) == one["result"]
    assert "ratitrust.reproduce" in one["reproduce"]
    assert client.get("/api/ratification/stock/GAU/999.json").status_code == 404
    assert client.get("/api/ratification/sports/X").status_code == 404
