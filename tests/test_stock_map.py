import gzip
import json
import re

import pytest
from fastapi.testclient import TestClient

from runner_watch.edgar import EdgarFiling, parse_beneficial_ownership_xml, parse_ownership_xml
from runner_web import db, intelligence, main
from runner_web.stock_map import filing_events, restore_archived_map_evidence, ticker_map
from tests.test_market_screens import _render_template, sample


def score_current(**changes):
    return {
        **sample("stocks"),
        "ticker": "TEST",
        "score": 45,
        "captured_at": "2026-09-12T18:00:00+00:00",
        "event_at": "2026-09-11T18:00:00+00:00",
        "score_detail": {
            "score": 45,
            "drivers": [
                {"key": "market", "label": "Market scanner", "value": 60},
                {"key": "sec_event", "label": "SEC events", "value": 30},
                {"key": "news", "label": "News", "value": 10},
                {"key": "community", "label": "Community", "value": 0},
                {"key": "social_search", "label": "Social / search", "value": -5},
            ],
            "penalties": [{"key": "rug", "label": "Rug risk", "value": -50}],
        },
        **changes,
    }


@pytest.mark.parametrize("score", [45, 0, None])
def test_unified_score_template_keeps_score_and_lists_filings(score):
    current = score_current(score=score)
    html = _render_template(
        "simple_stock_detail.html",
        {
            "detail": {
                "ticker": "TEST",
                "company": "Test Company",
                "current": current,
                "events": [filing_row()],
            },
            "active_call": None,
            "calls": [],
        },
    )
    assert 'class="map-score-heading"' not in html
    assert 'class="metrics"' not in html
    assert 'class="breakdown"' not in html
    assert re.search(r'<div class="map-filings">', html)
    assert "data-map-score-return hidden" in html
    assert 'aria-live="polite" aria-atomic="true"' in html
    for removed in (
        "data-map-score-time",
        "data-map-time-slider",
        "data-map-filter",
        "data-map-latest",
        "data-map-coverage",
        "map-heading",
        "data-chart-summary",
        "data-chart-start",
        "data-chart-end",
    ):
        assert removed not in html
    payload = json.loads(re.search(r'id="screenData">(.*?)</script>', html).group(1))
    assert payload["item"]["score"] == score
    assert payload["item"]["score_detail"] == current["score_detail"]
    fallback = re.search(r"<noscript>(.*?)</noscript>", html, re.S).group(1)
    assert filing_row()["filing_url"] in fallback
    assert "Recent company filings" not in html


def ownership_xml():
    owners = "".join(
        f"""<reportingOwner><reportingOwnerId><rptOwnerCik>{cik}</rptOwnerCik>
    <rptOwnerName>{name}</rptOwnerName></reportingOwnerId><reportingOwnerRelationship>
    <isDirector>1</isDirector><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle>
    </reportingOwnerRelationship></reportingOwner>"""
        for cik, name in [(101, "Jane Lee"), (102, "Lee Family, LLC")]
    )
    lines = "".join(
        f"""<nonDerivativeTransaction><securityTitle><value>Common stock</value>
    </securityTitle><transactionDate><value>2026-09-0{i + 1}</value></transactionDate>
    <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>{shares}</value></transactionShares>
    <transactionPricePerShare>{price}</transactionPricePerShare>
    <transactionAcquiredDisposedCode><value>{direction}</value></transactionAcquiredDisposedCode>
    </transactionAmounts><postTransactionAmounts><sharesOwnedFollowingTransaction><value>0</value>
    </sharesOwnedFollowingTransaction></postTransactionAmounts><footnoteId id="F1"/>
    </nonDerivativeTransaction>"""
        for i, (code, shares, price, direction) in enumerate(
            [
                ("P", "100", "<value>2</value>", "A"),
                ("S", "20", "<value>3</value>", "D"),
                ("A", "10", "", "A"),
            ]
        )
    )
    derivative = """<derivativeTable><derivativeTransaction><securityTitle><value>Option</value>
    </securityTitle><transactionDate><value>2026-09-04</value></transactionDate>
    <transactionCoding><transactionCode>M</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>25</value></transactionShares>
    <transactionPricePerShare><value>0</value></transactionPricePerShare></transactionAmounts>
    </derivativeTransaction></derivativeTable>"""
    return f"""<ownershipDocument xmlns="urn:sec"><issuer><issuerCik>22</issuerCik>
    <issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>{owners}<nonDerivativeTable>{lines}
    </nonDerivativeTable>{derivative}<footnotes><footnote id="F1">Held through the family trust.
    </footnote></footnotes></ownershipDocument>"""


def filing_row(accession="0001", ticker="TEST", **changes):
    return {
        "accession": accession,
        "cik": 22,
        "ticker": ticker,
        "company": "Test Company",
        "form": "4",
        "kind": "Insider filing",
        "sentiment": "neutral",
        "score": 20,
        "title": "Test filing",
        "filed_at": "2026-09-05T18:00:00+00:00",
        "filing_url": f"https://www.sec.gov/Archives/edgar/data/22/{accession}/index.htm",
        "created_at": "2026-09-05T18:05:00+00:00",
        "updated_at": "2026-09-05T18:05:00+00:00",
        "actor": "Jane Lee",
        "actor_cik": 101,
        "actor_title": "Director",
        "transaction_codes": "P,S",
        "transaction_value": 200,
        **changes,
    }


def evidence():
    parsed = parse_ownership_xml(ownership_xml())
    return {
        "version": 1,
        "owners": parsed.reporting_owners,
        "transactions": parsed.transactions,
        "positions": [],
    }


def insert(row):
    with db.connection() as conn:
        conn.execute(
            f"INSERT INTO sec_filings({','.join(row)}) VALUES({','.join('?' for _ in row)})",
            tuple(row.values()),
        )


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "maps.db")
    db.init_db()


def test_transaction_lines_preserve_joint_filers_dates_units_and_zero():
    parsed = parse_ownership_xml(ownership_xml())
    assert [p["cik"] for p in parsed.reporting_owners] == [101, 102]
    assert parsed.reporting_owners[0]["role"] == "Director · CEO"
    events = filing_events(filing_row(evidence_json=json.dumps(evidence())))
    assert [e["action"] for e in events] == [
        "Bought",
        "Sold",
        "Award or grant",
        "Exercise or conversion",
    ]
    assert [e["value"] for e in events] == [200, 60, None, 0]
    assert events[0]["post_shares"] == 0
    assert events[0]["occurred_at"] == "2026-09-01"
    assert events[0]["filed_at"] == "2026-09-05T18:00:00+00:00"
    assert events[0]["joint"] and len(events[0]["people"]) == 2
    assert events[0]["people"][1]["name"] == "Lee Family, LLC"
    assert events[0]["footnotes"] == "Held through the family trust."
    assert events[-1]["security_type"] == "derivative"


def test_ownership_rows_keep_each_person_and_class_with_literal_numbers():
    text = """<edgarSubmission xmlns="urn:sec"><formData><coverPageHeader>
    <issuerInfo><issuerCIK>22</issuerCIK></issuerInfo>
    <securitiesClassTitle>Class B</securitiesClassTitle>
    <dateOfEvent>09/01/2026</dateOfEvent></coverPageHeader><reportingPersons>
    <reportingPersonInfo><reportingPersonName>Fund, LP</reportingPersonName>
    <reportingPersonCIK>123</reportingPersonCIK><aggregateAmountOwned>5,000</aggregateAmountOwned>
    <percentOfClass>7.5</percentOfClass></reportingPersonInfo>
    <reportingPersonInfo><reportingPersonName>Manager LLC</reportingPersonName>
    <aggregateAmountOwned>5000</aggregateAmountOwned><percentOfClass>See row 11</percentOfClass>
    </reportingPersonInfo></reportingPersons></formData></edgarSubmission>"""
    parsed = parse_beneficial_ownership_xml(text)
    assert parsed.issuer_cik == 22
    assert parsed.owner_names == ("Fund, LP", "Manager LLC")
    assert [p["shares"] for p in parsed.positions] == [5000, 5000]
    assert [p["percent"] for p in parsed.positions] == [7.5, None]
    events = filing_events(
        filing_row(form="SCHEDULE 13D/A", evidence_json=json.dumps({"positions": parsed.positions}))
    )
    assert len(events) == 2
    assert events[0]["security"] == "Class B"
    assert events[0]["occurred_at"] == "2026-09-01"
    assert events[0]["amendment"]
    assert events[0]["people"][0]["id"] == "sec:123"


def test_old_mixed_summary_keeps_both_actions_and_source_boundary():
    event = filing_events(
        filing_row(filing_url="https://www.sec.gov.attacker.test/Archives/edgar/data/")
    )[0]
    assert event["action"] == "Bought and sold"
    assert event["value"] is None
    assert event["occurred_at"] is None
    assert event["source_url"] is None
    assert event["basis"] == "Filing summary"


def test_schedule_13g_cover_page_fields_and_collector_form_names():
    xml = """<edgarSubmission><formData><coverPageHeader>
    <securitiesClassTitle>Common</securitiesClassTitle>
    <dateOfEvent>09/01/2026</dateOfEvent></coverPageHeader><coverPageHeaderReportingPersonDetails>
    <reportingCik>123</reportingCik><reportingPersonName>Fund</reportingPersonName>
    <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>12345
    </reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
    <classPercent>4.5</classPercent></coverPageHeaderReportingPersonDetails>
    </formData></edgarSubmission>"""
    summary = parse_beneficial_ownership_xml(xml)
    assert summary.positions[0]["cik"] == 123
    assert summary.positions[0]["shares"] == 12345
    assert summary.positions[0]["percent"] == 4.5
    assert intelligence._interesting("SCHEDULE 13G/A")
    assert intelligence._interesting("SCHEDULE 13D")


def test_new_parser_revisits_form_names_ignored_by_previous_version(database):
    from runner_web.ingestion import mark_source_item

    mark_source_item(
        source="sec", feed="filing", item_key="old", status="ignored", parser_version="5"
    )
    assert not intelligence._already_seen("old")
    mark_source_item(
        source="sec",
        feed="filing",
        item_key="old",
        status="ignored",
        parser_version=intelligence.PARSER_VERSION,
    )
    assert intelligence._already_seen("old")


def test_ticker_pagination_covers_tied_dates_and_ignores_other_tickers(database):
    for i in range(55):
        insert(filing_row(f"{i:05}", "TEST"))
        insert(filing_row(f"x{i:05}", "ELSE"))
    first = ticker_map("TEST")
    second = ticker_map("TEST", first["next_cursor"])
    assert first["coverage"]["filings"] == 55
    assert first["loaded_filings"] == 50 and second["loaded_filings"] == 5
    assert second["next_cursor"] is None
    ids = [e["id"] for e in first["events"] + second["events"]]
    assert len(set(ids)) == 55
    assert all(not event_id.startswith("x") for event_id in ids)
    with pytest.raises(ValueError, match="cursor"):
        ticker_map("TEST", "invalid")


def test_collect_and_restore_use_the_same_durable_records(database, monkeypatch):
    parsed = parse_ownership_xml(ownership_xml())
    row = filing_row()
    filing = EdgarFiling(
        row["accession"], 22, "4", row["title"], "Issuer", row["filed_at"], row["filing_url"]
    )
    client = type(
        "Client",
        (),
        {"latest_filings": lambda self: [filing], "ownership_summary": lambda self, f: parsed},
    )()
    monkeypatch.setattr(intelligence, "EdgarClient", lambda **kw: client)
    monkeypatch.setattr(intelligence, "refresh_company_map", lambda c: 1)
    monkeypatch.setattr(
        intelligence, "_company_for_cik", lambda c: {"ticker": "TEST", "name": "Test Company"}
    )
    monkeypatch.setattr(intelligence, "_market_context", lambda tickers: {})
    monkeypatch.setattr(intelligence, "_companyfacts_candidate", lambda events: None)
    intelligence.refresh_edgar()
    expected = ticker_map("TEST")["events"]
    assert len(expected) == 4
    with db.connection() as conn:
        conn.execute("UPDATE sec_filings SET evidence_json=NULL")
        conn.execute("DELETE FROM worker_state WHERE key='stock_map_restore_after'")
        for name, body in [("index.json", b"{}"), ("form.xml", ownership_xml().encode())]:
            conn.execute(
                """INSERT INTO source_documents(source,source_url,content_hash,content_type,
            content_encoding,content,first_collected_at,last_collected_at)
            VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "sec",
                    row["filing_url"].replace("index.htm", name),
                    name,
                    "application/xml",
                    "gzip",
                    gzip.compress(body),
                    row["created_at"],
                    row["created_at"],
                ),
            )
    assert restore_archived_map_evidence() == 1
    assert ticker_map("TEST")["events"] == expected
    assert restore_archived_map_evidence() == 0


def test_public_endpoint_and_stable_sec_identity(database):
    first = evidence()
    second = evidence()
    second["owners"][0]["name"] = "Jane A. Lee"
    insert(filing_row("a", evidence_json=json.dumps(first)))
    insert(filing_row("b", evidence_json=json.dumps(second), form="4/A"))
    client = TestClient(main.app)
    try:
        response = client.get("/api/stocks/TEST/map")
        assert response.status_code == 200
        events = response.json()["events"]
        assert {e["people"][0]["id"] for e in events} == {"sec:101"}
        assert len(events) == 8
        assert "market_score" not in response.text
        assert client.get("/api/stocks/TEST/map?cursor=invalid").status_code == 400
    finally:
        client.close()


def test_person_connections_match_joint_cik_and_preserve_separate_interests(database):
    from runner_web.stock_map import person_connections

    insert(filing_row("source", evidence_json=json.dumps(evidence())))
    data = evidence()
    data["owners"][0]["name"] = "Jane Renamed"
    insert(filing_row("other", ticker="OTHER", actor_cik=102, evidence_json=json.dumps(data)))
    # A matching name or a number elsewhere in JSON is only a candidate.
    data["owners"] = [{"cik": 1101, "name": "Jane Lee", "role": "Director"}]
    insert(filing_row("unrelated", ticker="FALSE", actor_cik=1101, evidence_json=json.dumps(data)))
    result = person_connections("TEST", "sec:101")
    assert {e["ticker"] for e in result["events"]} == {"TEST", "OTHER"}
    assert {e["action"] for e in result["events"]} >= {"Bought", "Sold"}
    assert all(e["joint"] for e in result["events"])
    assert all(e["source_url"] for e in result["events"])
    assert result["identity_scope"] == "SEC CIK"


def test_person_connections_stake_owner_cik_and_ticker_scoped_names(database):
    from runner_web.stock_map import person_connections

    payload = {
        "positions": [
            {"name": "A Fund", "cik": 991, "percent": 17, "shares": 200, "security": "Class A"}
        ]
    }
    insert(
        filing_row("stake", ticker="FUND", form="SCHEDULE 13D/A", evidence_json=json.dumps(payload))
    )
    assert person_connections("TEST", "sec:991")["events"][0]["percent"] == 17
    insert(filing_row("named", actor_cik=None, transaction_codes="P"))
    insert(filing_row("same-name", ticker="OTHER", actor_cik=None, transaction_codes="P"))
    identity = filing_events(filing_row("named", actor_cik=None))[0]["people"][0]["id"]
    result = person_connections("TEST", identity)
    assert {e["ticker"] for e in result["events"]} == {"TEST"}
    assert result["identity_scope"] == "This ticker"


def test_person_connections_candidate_paging_and_invalid_input(database):
    from runner_web.stock_map import person_connections

    for accession in ("a", "b", "c"):
        insert(filing_row(accession, transaction_codes="P"))
    first = person_connections("TEST", "sec:101", limit=1)
    second = person_connections("TEST", "sec:101", first["next_cursor"], limit=1)
    assert first["events"][0]["accession"] == "c"
    assert second["events"][0]["accession"] == "b"
    with pytest.raises(ValueError):
        person_connections("TEST", "sec:101", "bad cursor")
    with pytest.raises(ValueError):
        person_connections("TEST", "sec:0")
    response = TestClient(main.app).get(
        "/api/stocks/TEST/map/connections", params={"person_id": "sec:101"}
    )
    assert response.status_code == 200
    assert len(response.json()["events"]) == 3


def test_wallet_portfolio_uses_shared_stock_rows_and_keeps_event_lines(database, monkeypatch):
    insert(filing_row("source", evidence_json=json.dumps(evidence())))
    insert(filing_row("other", ticker="USO", evidence_json=json.dumps(evidence())))
    monkeypatch.setattr(main, "_direct_ticker_item", lambda ticker, _: {
        **score_current(), "ticker": ticker, "company": f"{ticker} company"
    })
    response = TestClient(main.app).get("/wallets/stocks/TEST/sec:101")
    assert response.status_code == 200
    html = response.text
    assert "Jane Lee" in html
    assert 'class="ticker-list market-stocks"' in html
    assert html.count('class="ticker"') == 2
    assert html.count('class="wallet-event"') == 8
    assert 'href="/t/USO"' in html
    assert "Filed 2026-09-05" in html
    assert "% of class" not in html
    assert "sec.gov/Archives" in html
    assert TestClient(main.app).get("/wallets/stocks/TEST/invalid").status_code == 400
