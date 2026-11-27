from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from runner_watch.massive_data import MassiveDailyCache
from runner_web import dash, dash_research, db, macro_feed
from runner_web.db import connection, init_db


@pytest.fixture(autouse=True)
def macro_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "macro.sqlite3")
    init_db()


def test_fred_persists_dated_observations_and_keeps_other_series_after_a_failure():
    now = datetime.now(UTC)
    today = now.date()
    latest = today - timedelta(days=2)
    previous = today - timedelta(days=3)
    month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    previous_month = (month - timedelta(days=1)).replace(day=1)

    def download(url):
        series = parse_qs(urlsplit(url).query)["id"][0]
        points = {
            "DFF": [(latest, 3.75), (previous, 3.75)],
            "DGS2": [(latest, 4.25), (previous, 4.15)],
            "DGS10": [(latest, 4.75), (previous, 4.65)],
            "BAA10Y": [(latest, 1.6), (previous, 1.55)],
            "CPIAUCSL": [(month, 301), (previous_month, 300)],
            "CPIAUCNS": [(month, 312), (month.replace(year=month.year - 1), 300)],
        }
        if series == "AAA10Y":
            raise OSError("provider unavailable")
        return (
            f"observation_date,{series}\n"
            + "".join(f"{day},{value}\n" for day, value in points[series])
            + f"{today + timedelta(days=1)},999\n"
        ).encode()

    status = macro_feed.refresh_fred(at=now, download=download)
    assert status["AAA10Y"] == "unavailable"
    assert status["BAA10Y"] == "ok"
    snapshot = macro_feed.macro_snapshot()
    assert snapshot["status"] == "partial"
    assert snapshot["rates"]["treasury_2y"]["observation_date"] == latest.isoformat()
    assert snapshot["rates"]["treasury_2y"]["change_bps"] == 10
    assert snapshot["rates"]["curve_2s10s"]["value"] == 50
    assert snapshot["credit"]["baa_treasury_spread"]["value"] == 160
    assert snapshot["credit"]["baa_treasury_spread"]["unit"] == "basis_points"
    assert snapshot["credit"]["aaa_treasury_spread"]["status"] == "unavailable"
    assert snapshot["inflation"]["headline_month_over_month"]["change_pct"] == 0.33
    assert snapshot["inflation"]["headline_year_over_year"]["change_pct"] == 4.0
    assert snapshot["inflation"]["headline_year_over_year"]["source_url"].endswith("/CPIAUCNS")
    assert macro_feed.macro_snapshot(now)["status"] == "unavailable"
    macro_feed.refresh_fred(download=download)
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM macro_observations").fetchone()[0] == 12


def test_cpi_returns_partial_if_the_exact_comparison_month_is_missing():
    today = date(2026, 11, 27)
    values = {
        date(2026, 10, 1): {"value": 330, "first_collected_at": "2026-11-10T13:30:00+00:00"},
        date(2026, 8, 1): {"value": 320, "first_collected_at": "2026-09-10T12:30:00+00:00"},
    }
    reading = macro_feed._inflation_rate("CPIAUCSL", values, today)
    assert reading["status"] == "partial"
    assert reading["comparison_month"] == "2026-09"
    assert reading["change_pct"] is None


def test_cpi_keeps_the_last_comparable_annual_rate_on_its_original_month():
    values = {
        date(2026, 10, 1): {"value": 337.511, "first_collected_at": "2026-11-10T13:30:00+00:00"},
        date(2026, 9, 1): {"value": 336.940, "first_collected_at": "2026-10-10T12:30:00+00:00"},
        date(2025, 9, 1): {"value": 324.800, "first_collected_at": "2025-10-10T12:30:00+00:00"},
    }
    reading = macro_feed._inflation_rate("CPIAUCNS", values, date(2026, 11, 27), annual=True)
    assert reading["observation_month"] == "2026-10"
    assert reading["comparison_month"] == "2025-10"
    assert reading["change_pct"] is None
    assert reading["last_comparable"]["observation_month"] == "2026-09"
    assert reading["last_comparable"]["change_pct"] == 3.74


def test_breadth_uses_the_full_matched_close_population_and_skips_thanksgiving(
    tmp_path, monkeypatch
):
    path = tmp_path / "massive.sqlite3"
    monkeypatch.setenv("MASSIVE_CACHE_PATH", str(path))
    day = date(2026, 11, 27)
    prior = date(2026, 11, 25)
    close = datetime(2026, 11, 27, 18, tzinfo=UTC)
    checked = close + timedelta(minutes=30)
    cache = MassiveDailyCache(path)
    before = [{"T": f"T{i}", "c": 10, "v": 10} for i in range(200)]
    after = [
        {"T": f"T{i}", "c": 11 if i < 110 else 9 if i < 190 else 10, "v": 10} for i in range(200)
    ]
    after.extend([{"T": "NEW", "c": 10, "v": 10}, {"T": "OTC", "c": 2, "v": 4, "otc": True}])
    cache.store_date(prior, before)
    cache.store_date(day, after)
    cache.close()
    with sqlite3.connect(path) as database:
        database.executemany(
            "UPDATE fetched_dates SET fetched_at=? WHERE session_date=?",
            (
                ("2026-11-25T22:00:00+00:00", prior.isoformat()),
                ("2026-11-27T18:10:00+00:00", day.isoformat()),
            ),
        )
    persist = macro_feed.record_source_batch

    def record_at(batch):
        return persist(replace(batch, fetch=replace(batch.fetch, finished_at=checked)))

    monkeypatch.setattr(macro_feed, "record_source_batch", record_at)
    assert macro_feed.refresh_breadth(at=close - timedelta(seconds=1)) == 0
    assert macro_feed.refresh_breadth(at=checked) == 1
    assert macro_feed.refresh_breadth(at=checked) == 0
    result = macro_feed.macro_snapshot(checked, section="breadth")["breadth"]
    assert result["status"] == "available"
    assert result["session_date"] == day.isoformat()
    assert result["previous_session_date"] == prior.isoformat()
    assert result["session_close_at"] == close.isoformat()
    assert (result["advancing"], result["declining"], result["unchanged"]) == (110, 80, 10)
    assert result["matched_symbols"] == 200
    assert result["eligible_symbols"] == 201
    assert result["advancing_pct"] == 55
    assert result["coverage_pct"] == 99.5
    monday = datetime(2026, 11, 30, 22, tzinfo=UTC)
    assert macro_feed.macro_snapshot(monday, section="breadth")["breadth"]["status"] == "lagging"


def test_dash_expands_macro_and_its_sections():
    assert dash_research.canonical_node("Macro:Credit") == "macro:credit"
    assert dash_research.canonical_node("macro") == "macro"
    assert (
        dash.dash_expand("macro:credit")["credit"]["baa_treasury_spread"]["status"] == "unavailable"
    )
    assert dash.dash_expand("macro")["scope"] == "United States"
    assert "error" in dash.dash_expand("macro:invented")
