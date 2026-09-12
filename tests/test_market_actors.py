from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from runner_web import actor_portraits
from runner_web import db as db_module
from runner_web import main as web_main
from runner_web.db import connection, init_db
from runner_web.market_actors import (
    coin_subject_key,
    derive_coin_actors,
    derive_stock_actors,
    market_actor_comment_budget,
    market_actor_detail,
    market_actor_map,
    record_market_actor_comment,
)
from runner_web.pseudonyms import COMMENT_AVATAR_ABILITIES, derive_actor_identity

AT = datetime(2026, 9, 12, 15, tzinfo=UTC)
FILED_AT = AT.isoformat()
ABILITY_IDS = {str(ability["id"]) for ability in COMMENT_AVATAR_ABILITIES}


def _use_database(tmp_path: Path, monkeypatch: MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(db_module, "DATABASE_PATH", tmp_path / name)
    init_db()


def _insert_filing(
    accession: str,
    ticker: str,
    *,
    actor: str,
    actor_title: str = "Chief Financial Officer",
    transaction_codes: str = "P",
    transaction_value: float = 2_100_000.0,
    filed_at: str = FILED_AT,
    form: str = "4",
    beneficial_owner_names: str = "",
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,actor,actor_title,transaction_codes,transaction_value,
                beneficial_owner_names,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                accession,
                1,
                ticker,
                f"{ticker} Company",
                form,
                "Insider open-market buy",
                "positive",
                70.0,
                f"{form} - {ticker} Company",
                filed_at,
                f"https://www.sec.gov/{accession}",
                actor,
                actor_title,
                transaction_codes,
                transaction_value,
                beneficial_owner_names,
                filed_at,
                filed_at,
            ),
        )


def test_actor_identity_is_stable_and_fictional() -> None:
    first = derive_actor_identity("insider:jane-q-officer")
    second = derive_actor_identity("insider:jane-q-officer")

    assert first == second
    assert first["name"] != derive_actor_identity("insider:someone-else")["name"]
    assert first["ability_id"] in ABILITY_IDS
    assert len(first["seed"]) == 64
    assert "jane" not in first["name"].casefold()


def test_stock_derivation_creates_a_character_and_a_tie(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "stock-actors.db")
    _insert_filing("acc-one", "RUNR", actor="Jane Q. Officer")

    inserted = derive_stock_actors(at=AT)
    assert inserted == 1

    board = market_actor_map("stock", at=AT)
    assert board["counts"]["actors"] == 1
    assert board["counts"]["subjects"] == 1
    assert board["subjects"][0]["key"] == "RUNR"
    assert board["subjects"][0]["url"] == "/t/RUNR"
    actor = board["actors"][0]
    assert actor["kind"] == "person"
    assert actor["roles"] == ["officer"]
    assert "jane" not in json.dumps(board).casefold()

    detail = market_actor_detail(actor["id"])
    assert detail is not None
    assert detail["actor"]["roles"] == ["officer"]
    assert detail["evidence"][0]["evidence_kind"] == "sec_filing"
    assert detail["evidence"][0]["url"].endswith("acc-one")
    assert "jane" not in json.dumps(detail).casefold()


def test_beneficial_owner_gets_a_role_and_ownership_direction(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "owner-actors.db")
    _insert_filing(
        "acc-13d",
        "RUNR",
        actor="",
        actor_title="",
        transaction_codes="",
        form="SC 13D",
        beneficial_owner_names="Big Fund LP, Second Holder",
    )

    assert derive_stock_actors(at=AT) == 2
    board = market_actor_map("stock", at=AT)
    assert board["counts"]["actors"] == 2
    assert all("ten_percent_owner" in actor["roles"] for actor in board["actors"])
    assert {link["direction"] for link in board["links"]} == {"own"}


def test_stock_derivation_can_be_bounded_to_board_tickers(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "stock-tickers.db")
    _insert_filing("acc-one", "RUNR", actor="Jane Q. Officer")
    _insert_filing("acc-two", "OTHR", actor="Jane Q. Officer")

    assert derive_stock_actors(at=AT, tickers=["RUNR"]) == 1
    board = market_actor_map("stock", at=AT)
    assert {subject["key"] for subject in board["subjects"]} == {"RUNR"}

    assert derive_stock_actors(at=AT, tickers=["OTHR"]) == 1
    board = market_actor_map("stock", at=AT)
    assert {subject["key"] for subject in board["subjects"]} == {"RUNR", "OTHR"}



def test_coin_derivation_groups_wallets_into_one_cluster(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "coin-actors.db")
    forensics = {
        "analyzed_events": 12,
        "findings": [
            {
                "kind": "common_funder",
                "token_address": "MintAddress1111",
                "wallet": "FunderWallet",
                "related_wallets": ["BuyerOne", "BuyerTwo"],
                "signature": "signature-one",
                "observed_at": FILED_AT,
                "source_url": "https://solscan.io/tx/signature-one",
            }
        ],
    }
    with connection() as database:
        database.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES('memecoin_forensics',?,?)
            """,
            (json.dumps(forensics), FILED_AT),
        )

    assert derive_coin_actors(at=AT) == 1
    board = market_actor_map("coin", at=AT)
    assert board["counts"]["actors"] == 1
    assert board["subjects"][0]["key"] == coin_subject_key("MintAddress1111")
    assert board["actors"][0]["kind"] == "cluster"
    assert board["actors"][0]["roles"] == ["funder"]

    detail = market_actor_detail(board["actors"][0]["id"])
    assert detail is not None
    assert sorted(detail["actor"]["wallets"]) == ["BuyerOne", "BuyerTwo", "FunderWallet"]
    assert detail["evidence"][0]["url"] == "/api/memecoins/evidence/signature-one"


def test_actor_comment_budget_counts_per_actor_and_globally(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "actor-budget.db")
    _insert_filing("acc-one", "RUNR", actor="Jane Q. Officer")
    derive_stock_actors(at=AT)
    board = market_actor_map("stock", at=AT)
    actor = board["actors"][0]

    assert market_actor_comment_budget(actor["id"], at=AT)["allowed"] is True
    record_market_actor_comment(at=AT)
    assert market_actor_comment_budget(actor["id"], at=AT)["global_remaining"] == 299


def _png_data_url() -> str:
    return "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n fake").decode()


def test_portrait_generation_caches_the_image_and_respects_budget(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "actor-portraits.db")
    _insert_filing("acc-one", "RUNR", actor="Jane Q. Officer")
    _insert_filing("acc-two", "RUNR", actor="Second Person")
    derive_stock_actors(at=AT)
    actors = market_actor_map("stock", at=AT)["actors"]
    assert len(actors) == 2

    monkeypatch.setattr(actor_portraits, "DAILY_LIMIT", 5)
    calls: list[str] = []

    def transport(key: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append(key)
        assert payload["modalities"] == ["image", "text"]
        return {
            "choices": [{"message": {"images": [{"image_url": {"url": _png_data_url()}}]}}]
        }

    first = actor_portraits.generate_actor_portrait(
        actors[0]["id"], api_key="sk-or-test", transport=transport, at=AT
    )
    assert first == {"status": "ready"}
    stored = actor_portraits.portrait_for_actor(actors[0]["id"])
    assert stored is not None
    assert stored["content_type"] == "image/png"
    assert stored["bytes"].startswith(b"\x89PNG")

    cached = actor_portraits.generate_actor_portrait(
        actors[0]["id"], api_key="sk-or-test", transport=transport, at=AT
    )
    assert cached == {"status": "ready"}
    assert calls == ["sk-or-test"]

    monkeypatch.setattr(actor_portraits, "DAILY_LIMIT", 0)
    second = actor_portraits.generate_actor_portrait(
        actors[1]["id"], api_key="sk-or-test", transport=transport, at=AT
    )
    assert second["status"] == "fallback"
    assert second["reason"] == "budget"


def test_portrait_falls_back_without_a_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "actor-portraits-nokey.db")
    _insert_filing("acc-one", "RUNR", actor="Jane Q. Officer")
    derive_stock_actors(at=AT)
    actor = market_actor_map("stock", at=AT)["actors"][0]

    result = actor_portraits.generate_actor_portrait(actor["id"], api_key="", at=AT)
    assert result == {"status": "fallback", "reason": "no_key"}
    assert actor_portraits.portrait_for_actor(actor["id"]) is None


def test_portrait_fallback_enforces_a_retry_cooldown(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "actor-portraits-cooldown.db")
    _insert_filing("acc-one", "RUNR", actor="Jane Q. Officer")
    derive_stock_actors(at=AT)
    actor = market_actor_map("stock", at=AT)["actors"][0]
    monkeypatch.setattr(actor_portraits, "DAILY_LIMIT", 5)
    calls: list[str] = []

    def transport(key: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append(key)
        return {"choices": [{"message": {"content": "no image here"}}]}

    first = actor_portraits.generate_actor_portrait(
        actor["id"], api_key="sk-or-test", transport=transport, at=AT
    )
    assert first["status"] == "fallback"
    again = actor_portraits.generate_actor_portrait(
        actor["id"], api_key="sk-or-test", transport=transport, at=AT
    )
    assert again == {"status": "fallback", "reason": "cooldown"}
    assert len(calls) == 1


async def _empty_body() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


def test_actor_comment_endpoint_posts_a_third_person_comment(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "actor-comment.db")
    _insert_filing("acc-one", "RUNR", actor="Jane Q. Officer")
    derive_stock_actors(at=AT)
    actor = market_actor_map("stock", at=AT)["actors"][0]
    timestamp = datetime.now(UTC)
    raw_session = "actor-comment-session"
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) "
            "VALUES(?,?,?,'active',?)",
            ("actor-requester", "member_requester", "Requester", timestamp.isoformat()),
        )
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                web_main.token_hash(raw_session),
                "actor-requester",
                timestamp.isoformat(),
                (timestamp + timedelta(days=1)).isoformat(),
            ),
        )
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "sk-or-actor-test")
    monkeypatch.setattr(
        web_main,
        "_generate_market_actor_comment_text",
        lambda actor_id, *, avatar: ("The officer filed a $2.1M open-market buy.", "test/model"),
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/api/market-actors/{actor['id']}/comment",
            "headers": [(b"origin", web_main.APP_ORIGIN.encode())],
            "client": ("127.0.0.79", 4781),
        },
        receive=_empty_body,
    )

    response = asyncio.run(
        web_main.create_market_actor_comment_api(actor["id"], request, raw_session)
    )
    assert response.status_code == 201
    detail = market_actor_detail(actor["id"])
    assert detail is not None
    assert detail["comments"][0]["body"] == "The officer filed a $2.1M open-market buy."
    assert detail["comments"][0]["author_kind"] == "ai_avatar"

    blocked = market_actor_comment_budget(actor["id"], at=datetime.now(UTC))
    assert blocked["actor_remaining"] == 2


def test_market_actor_api_serializes_the_map_and_detail(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _use_database(tmp_path, monkeypatch, "actor-api.db")
    _insert_filing("acc-one", "RUNR", actor="Jane Q. Officer")
    derive_stock_actors(at=AT)
    monkeypatch.setattr(web_main, "enforce_rate", lambda *_args, **_kwargs: None)
    client = TestClient(web_main.app)
    try:
        listing = client.get("/api/market-actors", params={"domain": "stock"})
        assert listing.status_code == 200
        payload = listing.json()
        assert payload["counts"]["actors"] == 1
        actor_id = payload["actors"][0]["id"]

        detail = client.get(f"/api/market-actors/{actor_id}")
        assert detail.status_code == 200
        assert detail.json()["actor"]["name"] == payload["actors"][0]["name"]

        missing = client.get("/api/market-actors/ma-does-not-exist")
        assert missing.status_code == 404
    finally:
        client.close()
