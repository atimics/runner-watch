from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from runner_web import db, trust


def test_the_published_record_commits_to_the_current_version_and_every_digest_checks_out():
    record = trust.rules_record()

    assert record["current"]["version"] == "2.1.0"
    assert record["current"]["digest"].startswith("78312512")
    assert record["sealed"] is True and record["shown"]["version"] == "2.0.0"
    assert "files" not in record["current"]  # the current version is only a commitment
    assert [version["version"] for version in record["revealed"]] == [
        "2.0.0",
        "1.0.2",
        "1.0.1",
        "1.0.0",
    ]
    assert all(version["verified"] for version in record["revealed"])
    assert "## Memecoins: nine standards" in record["revealed"][3]["rules_text"]


def test_rules_published_as_data_are_read_as_structured_standards():
    rules = trust.rules_record()["revealed"][0]["rules"]

    memecoin, stock = rules["assets"]["memecoin"], rules["assets"]["stock"]
    assert len(memecoin["standards"]) == 9 and len(stock["standards"]) == 5
    pool = next(item for item in memecoin["standards"] if item["id"] == "pool")
    assert pool["tests"] == ["venue is pool", "real_liquidity at least 10,000 USD"]
    cash = next(item for item in stock["standards"] if item["id"] == "cash")
    assert cash["tests"] == [
        "cash_runway at least 12 months, unless operating_cash_flow at least 0 USD"
    ]
    assert cash["not_applied_text"] == "sic between 6000 and 6799"
    financials = next(fact for fact in stock["facts"] if fact["id"] == "financials")
    assert "us-gaap:NetCashProvidedByUsedInOperatingActivities" in financials["xbrl"]


def test_a_tampered_file_no_longer_matches_its_digest():
    record = json.loads((trust.ASSETS / "trust-rules-1.0.0.json").read_text())
    record["files"]["RULES.md"] = record["files"]["RULES.md"].replace("30%", "60%")

    assert trust.recomputed_digest(record) != record["digest"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    from runner_web import main

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "trust.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    monkeypatch.setattr(main, "enforce_rate", lambda *_a, **_k: None)
    monkeypatch.setattr(main, "current_user", lambda *_: None)
    return TestClient(main.app, base_url=main.RUNNERS_ORIGIN, follow_redirects=False)


def test_the_trust_page_shows_the_commitment_and_the_rules(client):
    page = client.get("/trust")

    assert page.status_code == 200
    assert "78312512b3ca22a231e9ebe086fffb0b591552ee4a4a1a036cefbc361bc0a80a" in page.text
    assert "cd0357851f936c3628b12f65b7a62bf24cc0c02d40fe265dea26f98d622b029d" in page.text
    assert "Version 2.1.0 stays sealed until it is replaced" in page.text
    assert "Shown below is version 2.0.0, the newest published in full." in page.text
    assert "days_since_annual_report at most 490 days, when foreign_issuer is true" in page.text
    assert "does not match" not in page.text
    assert 'class="market-switcher"' not in page.text  # the rules are not a market
    assert "board-view-bar" not in page.text  # no List/Map bar on the rules
    assert "real_liquidity at least 10,000 USD" in page.text
    assert "us-gaap:CashAndCashEquivalentsAtCarryingValue" in page.text
    assert "## Memecoins: nine standards" in page.text  # 1.0.0 is still shown as it was
    assert "/trust/access" not in page.text and "Request access" not in page.text


def test_trust_rati_chat_serves_the_page_at_its_root(client):
    from runner_web import main

    trust_site = TestClient(main.app, base_url=main.TRUST_ORIGIN, follow_redirects=False)

    page = trust_site.get("/")

    assert page.status_code == 200 and "The RATi Rules" in page.text


def test_only_operations_can_list_requests(client):
    assert client.get("/api/trust/access-requests").status_code == 404


def test_the_published_rules_file_and_schema_are_served_as_revealed(client):
    from runner_web import main

    published = trust.rules_record()["revealed"][0]["files"]
    rules = client.get("/trust/rules.toml")
    schema = TestClient(main.app, base_url=main.TRUST_ORIGIN).get("/rules.schema.json")

    assert rules.status_code == 200 and rules.text == published["src/ratitrust/rules.toml"]
    assert rules.headers["content-type"].startswith("application/toml")
    assert schema.json()["$id"] == "https://trust.rati.chat/rules.schema.json"


def test_a_sealed_version_in_force_says_which_published_rules_are_shown(client, monkeypatch):
    record = trust.rules_record()
    sealed = {"version": "1.1.0", "released": "2026-10-01", "digest": "ab" * 32}
    monkeypatch.setattr(
        trust,
        "rules_record",
        lambda: {**record, "current": sealed, "shown": record["revealed"][0], "sealed": True},
    )

    page = client.get("/trust").text

    assert "Version 1.1.0 stays sealed until it is replaced" in page
    assert "Shown below is version 2.0.0, the newest published in full." in page
    assert "In force, sealed" in page and "ab" * 32 in page


def test_a_clause_that_applies_only_in_one_case_says_so():
    clause = {
        "fact": "days_since_annual_report",
        "op": "<=",
        "value": 490,
        "unit": "days",
        "when": {"fact": "foreign_issuer", "op": "==", "value": True},
    }

    assert trust.describe_clause(clause, {}) == (
        "days_since_annual_report at most 490 days, when foreign_issuer is true"
    )
