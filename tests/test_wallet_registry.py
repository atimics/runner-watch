"""Wallet ids: one hashed id per identity, over the participant identity layer."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from runner_web import db
from runner_web import main as web_main
from runner_web.wallet_registry import (
    register_people,
    register_person,
    wallet,
    wallet_id_for,
    wallet_id_for_person,
)
from tests.test_mobile import insert_scan_run, insert_scored_snapshot

SEC_PERSON = "sec:1475597"
NAME_PERSON = "name:" + "a" * 20


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "wallets.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    with db.connection() as connection:
        yield connection


def test_ids_are_stable_and_scoped():
    first = wallet_id_for_for_test = wallet_id_for(kind="sec", value="1475597")
    assert first == wallet_id_for_for_test
    assert re.fullmatch(r"w_[0-9a-f]{16}", first)
    # The same reported name in two tickers is two identities.
    assert wallet_id_for(kind="name", value="a" * 20, scope="AAPL") != wallet_id_for(
        kind="name", value="a" * 20, scope="BRR"
    )


def test_people_bridge_onto_wallet_ids():
    assert wallet_id_for_person(SEC_PERSON) == wallet_id_for(kind="sec", value="1475597")
    assert wallet_id_for_person(NAME_PERSON, "aapl") == wallet_id_for(
        kind="name", value="a" * 20, scope="AAPL"
    )
    assert wallet_id_for_person("invalid") is None
    assert wallet_id_for_person("name:nothex") is None


def test_registration_is_idempotent_and_resolvable(database):
    wallet_id = register_person(SEC_PERSON, database=database)
    assert wallet_id is not None
    assert register_person(SEC_PERSON, database=database) == wallet_id

    resolved = wallet(wallet_id, database=database)
    assert resolved is not None
    assert resolved["kind"] == "sec"
    assert resolved["person_id"] == SEC_PERSON
    assert resolved["scope"] == ""

    with database as conn:
        entities = conn.execute(
            "SELECT COUNT(*) AS total FROM participant_entities WHERE dedupe_key=?", (wallet_id,)
        ).fetchone()
        references = conn.execute(
            "SELECT COUNT(*) AS total FROM participant_references WHERE entity_id=?",
            (resolved["entity_id"],),
        ).fetchone()
    assert entities["total"] == 1
    assert references["total"] == 1


def test_a_reported_name_keeps_the_ticker_it_came_from(database):
    wallet_id = register_person(NAME_PERSON, "BRR", database=database)
    resolved = wallet(wallet_id, database=database)
    assert resolved is not None
    assert resolved["kind"] == "name"
    assert resolved["person_id"] == NAME_PERSON
    assert resolved["scope"] == "BRR"


def test_unknown_wallet_ids_are_none(database):
    assert wallet("w_" + "0" * 16, database=database) is None
    assert wallet("not-a-wallet", database=database) is None


def test_registering_a_page_of_people_dedupes_by_identity(database):
    events = [
        {
            "people": [
                {"id": SEC_PERSON},
                {"id": NAME_PERSON},
                {"id": SEC_PERSON},
            ]
        }
    ]
    assert register_people(events, "BRR", database=database) == 2
    with database as conn:
        total = conn.execute("SELECT COUNT(*) AS total FROM participant_entities").fetchone()
    assert total["total"] == 2


def _ticker_client(monkeypatch) -> TestClient:
    captured = datetime.now(UTC).isoformat()
    insert_scan_run("wallet-route-run", captured, 1)
    insert_scored_snapshot(
        "wallet-route-snapshot", "wallet-route-run", "ONE", 70, 1, captured, price=3.5
    )
    return TestClient(web_main.app)


def test_the_stock_path_is_canonical_and_the_shorthand_redirects(database, monkeypatch):
    client = _ticker_client(monkeypatch)

    page = client.get("/stock/ONE")
    assert page.status_code == 200
    assert "ONE" in page.text

    legacy = client.get("/t/ONE", follow_redirects=False)
    assert legacy.status_code == 301
    assert legacy.headers["location"] == "/stock/ONE"

    card = client.get("/stock/ONE/card.png")
    assert card.status_code == 200 and card.content[:8] == b"\x89PNG\r\n\x1a\n"
    card_legacy = client.get("/t/ONE/card.png", follow_redirects=False)
    assert card_legacy.status_code == 301
    assert card_legacy.headers["location"] == "/stock/ONE/card.png"


def test_a_wallet_page_needs_no_stock_in_its_path(database, monkeypatch):
    client = _ticker_client(monkeypatch)
    wallet_id = register_person(SEC_PERSON, database=database)
    assert wallet_id is not None
    database.commit()  # the route resolves it on its own connection

    response = client.get(f"/wallet/{wallet_id}")

    assert response.status_code == 200
    # A global identity has no stock context, so no back link to one.
    assert 'class="back"' not in response.text
    assert client.get("/wallet/w_" + "0" * 16).status_code == 404

    # A reported name still belongs to the ticker it was read from.
    scoped = register_person(NAME_PERSON, "ONE", database=database)
    database.commit()
    scoped_page = client.get(f"/wallet/{scoped}")
    assert scoped_page.status_code == 200
    assert 'href="/stock/ONE"' in scoped_page.text


def _entity_data(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="entityData"[^>]*>(.*?)</script>', html)
    assert match
    return json.loads(match.group(1))


def test_the_entity_map_covers_the_whole_record_not_the_first_page(database, monkeypatch):
    from runner_web import stock_map
    from tests.test_stock_map import evidence, filing_row, insert

    client = _ticker_client(monkeypatch)
    payload = json.dumps(evidence())
    for number in range(450):
        insert(
            filing_row(
                f"acc-{number:04d}",
                ticker=f"T{number:03d}",
                filed_at=f"2026-09-05T18:{number // 60:02d}:{number % 60:02d}+00:00",
                evidence_json=payload,
            )
        )
    wallet_id = register_person("sec:101", database=database)
    database.commit()

    page = client.get(f"/wallet/{wallet_id}")

    assert page.status_code == 200
    # One page of the list is 200 candidate filings; the map is every stock.
    assert len(_entity_data(page.text)["entity"]["stocks"]) == 450
    assert "All filings read" in page.text
    assert "data-wallet-more" in page.text  # the event list itself still pages

    monkeypatch.setattr(stock_map, "ENTITY_MAX_PAGES", 2)
    bounded = client.get(f"/wallet/{wallet_id}")
    assert len(_entity_data(bounded.text)["entity"]["stocks"]) == 400
    assert "Newest filings only" in bounded.text
