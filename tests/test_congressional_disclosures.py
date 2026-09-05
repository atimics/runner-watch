from __future__ import annotations

import io
import tomllib
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.congressional_disclosures import (
    HOUSE_INDEX_URL,
    HOUSE_PTR_URL,
    PARSER_VERSION,
    parse_house_filing_index,
    parse_house_ptr_text,
    refresh_house_disclosures,
    trades_to_market_events,
)
from runner_web.db import connection, init_db

PTR_TEXT = """
P T R
Name: Hon. Nancy Pelosi
Status: Member
State/District: CA11
ID Owner Asset Transaction
Type
Date Notification
Date
Amount Cap.
Gains >
$200?
SP Bloom Energy Corporation Class A
Common Stock (BE) [ST]
P 07/24/2026 07/24/2026 $1,000,001 -
$5,000,000
F S: New
D: Purchased 10,000 shares.
SP Bloom Energy Corporation Class A
Common Stock (BE) [OP]
P 07/24/2026 07/24/2026 $1,000,001 -
$5,000,000
F S: New
D: Purchased 100 call options with a strike price of $100 and an expiration date of 6/17/27.
SP Bloom Energy Corporation Class A
Common Stock (BE) [ST]
P 07/28/2026 07/28/2026 $500,001 -
$1,000,000
F S: New
D: Purchased 5,000 shares.
SP Bloom Energy Corporation Class A
Common Stock (BE) [OP]
P 07/28/2026 07/28/2026 $500,001 -
$1,000,000
F S: New
D: Purchased 100 call options with a strike price of $100 and an expiration date of 6/17/27.
SP Intel Corporation - Common Stock
(INTC) [OP]
P 07/24/2026 07/24/2026 $250,001 -
$500,000
F S: New
D: Purchased 50 call options with a strike price of $50 and an expiration date of 6/17/27.
SP Intel Corporation - Common Stock
(INTC) [ST]
P 07/24/2026 07/24/2026 $500,001 -
$1,000,000
F S: New
D: Purchased 10,000 shares.
Filing ID #20035143
---PAGE---
ID Owner Asset Transaction
Type
Date Notification
Date
Amount Cap.
Gains >
$200?
SP REOF XXV, LLC [AB] P 07/27/2026 07/27/2026 $500,001 -
$1,000,000
F S: New
D: Additional investment in LLC which is acquiring and restoring a luxury hotel property.
"""


def test_fly_deploy_enables_internal_house_collection() -> None:
    config = tomllib.loads((Path(__file__).parents[1] / "fly.toml").read_text())

    assert config["env"]["HOUSE_DISCLOSURES_ENABLED"] == "true"


def _index_zip() -> bytes:
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<FinancialDisclosure>
  <Member>
    <Prefix>Hon.</Prefix>
    <Last>Pelosi</Last>
    <First>Nancy</First>
    <Suffix></Suffix>
    <FilingType>P</FilingType>
    <StateDst>CA11</StateDst>
    <Year>2026</Year>
    <FilingDate>8/21/2026</FilingDate>
    <DocID>20035143</DocID>
  </Member>
  <Member>
    <Prefix>Hon.</Prefix>
    <Last>Earlier</Last>
    <First>Example</First>
    <Suffix></Suffix>
    <FilingType>P</FilingType>
    <StateDst>NY01</StateDst>
    <Year>2026</Year>
    <FilingDate>1/2/2026</FilingDate>
    <DocID>20000001</DocID>
  </Member>
</FinancialDisclosure>
"""
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (("2026FD.xml", xml), ("2026FD.txt", "official text index")):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    return body.getvalue()


def test_official_house_index_zip_is_parsed_without_extracting_files() -> None:
    filings = parse_house_filing_index(_index_zip(), expected_year=2026)

    assert len(filings) == 2
    filing = filings[0]
    assert filing.document_id == "20035143"
    assert filing.filing_type == "P"
    assert filing.filing_date == date(2026, 8, 21)
    assert filing.filer_name == "Hon. Nancy Pelosi"
    assert filing.state_district == "CA11"
    assert filing.source_url == HOUSE_PTR_URL.format(year=2026, document_id="20035143")


def test_house_ptr_parser_preserves_ranges_options_and_private_assets() -> None:
    trades = parse_house_ptr_text(PTR_TEXT)

    assert len(trades) == 7
    assert [trade.ticker for trade in trades] == ["BE", "BE", "BE", "BE", "INTC", "INTC", None]
    bloom_stock, bloom_option = trades[:2]
    assert bloom_stock.owner == "spouse"
    assert bloom_stock.amount_min == 1_000_001
    assert bloom_stock.amount_max == 5_000_000
    assert bloom_stock.shares == 10_000
    assert bloom_option.asset_type == "OP"
    assert bloom_option.option_kind == "call"
    assert bloom_option.option_contracts == 100
    assert bloom_option.option_strike == 100
    assert bloom_option.option_expiry == date(2027, 6, 17)

    intel_option = trades[4]
    assert intel_option.ticker == "INTC"
    assert intel_option.amount_min == 250_001
    assert intel_option.amount_max == 500_000
    assert intel_option.option_contracts == 50
    assert intel_option.option_strike == 50

    private_asset = trades[-1]
    assert private_asset.asset_name == "REOF XXV, LLC"
    assert private_asset.asset_type == "AB"
    assert private_asset.ticker is None


def test_market_events_use_conservative_filing_time_and_skip_private_assets() -> None:
    filing = parse_house_filing_index(_index_zip(), expected_year=2026)[0]
    events = trades_to_market_events(filing, parse_house_ptr_text(PTR_TEXT))

    assert len(events) == 6
    event = events[0]
    assert event.event_id == "20035143:1"
    assert event.event_type == "congressional_trade"
    assert event.event_at == datetime(2026, 8, 22, 3, 59, 59, 999999, tzinfo=UTC)
    assert event.published_at == event.event_at
    assert event.effective_at == datetime(2026, 7, 24, 4, tzinfo=UTC)
    assert event.payload["availability_time_known"] is False
    assert event.payload["filing_timestamp_precision"] == "date"
    assert event.payload["amount_is_range"] is True
    assert event.payload["parser_version"] == PARSER_VERSION
    assert event.source_url.endswith("/2026/20035143.pdf")


def test_refresh_archives_official_files_deduplicates_and_stays_internal(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "house-disclosures.db")
    init_db()
    index_url = HOUSE_INDEX_URL.format(year=2026)
    ptr_url = HOUSE_PTR_URL.format(year=2026, document_id="20035143")
    calls: list[str] = []

    def download(url: str, _timeout: float) -> tuple[bytes, str]:
        calls.append(url)
        if url == index_url:
            return _index_zip(), "application/zip"
        if url == ptr_url:
            return b"official-house-ptr-pdf", "application/pdf"
        raise AssertionError(f"unexpected URL: {url}")

    options = {
        "download": download,
        "parse_pdf": lambda _body: parse_house_ptr_text(PTR_TEXT),
        "now": datetime(2026, 8, 31, 18, tzinfo=UTC),
        "lookback_days": 14,
        "max_filings": 10,
    }
    first = refresh_house_disclosures(**options)
    second = refresh_house_disclosures(**options)

    assert first["status"] == "success"
    assert first["filings_considered"] == 1
    assert first["filings_downloaded"] == 1
    assert first["parsed_rows"] == 7
    assert first["events"] == 6
    assert first["ignored_rows_without_ticker"] == 1
    assert second["filings_downloaded"] == 0
    assert second["filings_skipped"] == 1
    assert calls == [index_url, ptr_url, index_url]

    with connection() as database:
        ingestion_runs = database.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
        documents = database.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
        events = database.execute("SELECT COUNT(*) FROM market_events").fetchone()[0]
        public_events = database.execute("SELECT COUNT(*) FROM public_market_events").fetchone()[0]
        item = database.execute(
            "SELECT status,attempt_count,parser_version FROM source_item_state"
        ).fetchone()
        policies = database.execute(
            "SELECT feed,review_status,display_policy FROM source_registry "
            "WHERE source='house_clerk' ORDER BY feed"
        ).fetchall()

    assert ingestion_runs == 3
    assert documents == 2
    assert events == 6
    assert public_events == 0
    assert tuple(item) == ("processed", 1, PARSER_VERSION)
    assert [tuple(policy) for policy in policies] == [
        ("house_filing_index", "review_required", "internal_review_only"),
        ("house_ptr", "review_required", "internal_review_only"),
    ]
