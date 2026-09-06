from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from runner_web import db, memecoins
from runner_web import main as web_main
from runner_web import memecoin_calls as call_service
from runner_web.calls import caller_summary_for_user, create_call
from runner_web.db import connection, init_db
from runner_web.privacy import delete_user_content, delete_user_data, export_user_data


@pytest.fixture
def calls_db(tmp_path: Path, monkeypatch: MonkeyPatch) -> dict[str, datetime]:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "memecoin-calls.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    monkeypatch.setenv("MEMECOINS_ENABLED", "true")
    clock = {"now": datetime.now(UTC)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setattr(memecoins, "datetime", Clock)
    monkeypatch.setattr(call_service, "datetime", Clock)
    monkeypatch.setattr(memecoins, "_download", lambda *_: pytest.fail("live provider request"))
    web_main.RATE_LIMITS.clear()
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    init_db()
    with connection() as database:
        for user_id in ("alice", "bob"):
            database.execute(
                "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, user_id, user_id.title(), "active", clock["now"].isoformat()),
            )
            database.execute(
                "INSERT INTO sessions(token_hash,user_id,created_at,expires_at,authenticated_at) "
                "VALUES(?,?,?,?,?)",
                (
                    web_main.token_hash(f"{user_id}-session"),
                    user_id,
                    clock["now"].isoformat(),
                    (clock["now"] + timedelta(days=1)).isoformat(),
                    clock["now"].isoformat(),
                ),
            )
    return clock


def _coin(clock: dict[str, datetime], coin_id: str = "dogecoin", **extra: Any) -> dict[str, Any]:
    return {
        "id": coin_id,
        "symbol": "doge",
        "name": coin_id.title(),
        "current_price": 0.12,
        "total_volume": 900_000,
        "market_cap": 18_000_000,
        "price_change_percentage_24h": 2.5,
        "last_updated": clock["now"].isoformat(),
        **extra,
    }


def _refresh(clock: dict[str, datetime], *coins: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(list(coins) or [_coin(clock)]).encode()
    result = memecoins.refresh_memecoins(download=lambda *_: body, at=clock["now"])
    assert result["status"] == "ok"
    return result


def _rows() -> list[dict[str, Any]]:
    with connection() as database:
        return [dict(row) for row in database.execute("SELECT * FROM memecoin_calls").fetchall()]


def _flash_rows(user_id: str) -> list[dict[str, Any]]:
    with connection() as database:
        return [
            dict(row)
            for row in database.execute(
                "SELECT amount,kind,reference_id FROM flash_transactions WHERE user_id=?",
                (user_id,),
            ).fetchall()
        ]


def test_entry_and_exit_freeze_source_receipts_and_repeated_requests(calls_db):
    first_run = _refresh(calls_db)
    entry_time = calls_db["now"].isoformat()
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    calls_db["now"] += timedelta(minutes=5)
    second_run = _refresh(calls_db, _coin(calls_db, current_price=0.15))
    repeated = call_service.create_memecoin_call("alice", "dogecoin")
    closed = call_service.close_memecoin_call("alice", opened["public_id"])

    assert repeated["public_id"] == opened["public_id"]
    assert repeated["entry_price"] == opened["entry_price"] == 0.12
    assert repeated["entry_evidence"] == opened["entry_evidence"]
    assert repeated["mark_price"] == 0.15
    assert opened["entry_evidence"] == {
        "coin_id": "dogecoin",
        "symbol": "DOGE",
        "name": "Dogecoin",
        "price": 0.12,
        "observed_at": entry_time,
        "collected_at": entry_time,
        "source_url": "https://www.coingecko.com/en/coins/dogecoin",
        "run_id": first_run["run_id"],
    }
    assert closed["status"] == "closed"
    assert closed["entry_at"] == entry_time
    assert closed["exit_at"] == calls_db["now"].isoformat()
    assert closed["exit_price"] == 0.15
    assert closed["return_pct"] == 25.0
    assert closed["exit_evidence"]["run_id"] == second_run["run_id"]
    assert closed["exit_evidence"]["price"] == 0.15
    assert closed["exit_evidence"]["observed_at"] == closed["exit_at"]
    assert closed["entry_evidence"] == opened["entry_evidence"]
    calls_db["now"] += timedelta(minutes=20)
    assert call_service.close_memecoin_call("alice", opened["public_id"]) == closed
    assert len(_rows()) == 1
    _refresh(calls_db)
    reopened = call_service.create_memecoin_call("alice", "dogecoin")
    assert reopened["public_id"] != opened["public_id"]
    assert reopened["status"] == "active"
    assert len(_rows()) == 2


def test_coin_ids_and_ownership_keep_calls_separate(calls_db):
    _refresh(calls_db, _coin(calls_db, "first"), _coin(calls_db, "second", current_price=0.3))
    first = call_service.create_memecoin_call("alice", "first")
    second = call_service.create_memecoin_call("alice", "second")
    other_owner = call_service.create_memecoin_call("bob", "first")

    assert first["symbol"] == second["symbol"] == "DOGE"
    assert len({item["public_id"] for item in (first, second, other_owner)}) == 3
    assert first["entry_price"] == 0.12 and second["entry_price"] == 0.3
    assert first["detail_url"] == "/memecoins/coin/first"
    assert second["detail_url"] == "/memecoins/coin/second"
    assert first["caller_handle"] == second["caller_handle"]
    assert first["caller_handle"] != other_owner["caller_handle"]
    assert call_service.close_memecoin_call("bob", first["public_id"]) is None
    assert call_service.active_memecoin_call("alice", "first")["public_id"] == first["public_id"]
    assert len(call_service.memecoin_calls(coin_id="first")) == 2
    assert all(row["status"] == "active" for row in _rows())


@pytest.mark.parametrize("state", ["stale", "future", "disabled"])
def test_create_and_close_require_a_fresh_enabled_source(calls_db, monkeypatch, state):
    _refresh(calls_db)
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    before = _rows()
    if state == "stale":
        calls_db["now"] += timedelta(minutes=16)
    elif state == "future":
        calls_db["now"] += timedelta(minutes=5)
        _refresh(
            calls_db,
            _coin(calls_db, last_updated=(calls_db["now"] + timedelta(hours=1)).isoformat()),
        )
    else:
        monkeypatch.setenv("MEMECOINS_ENABLED", "false")

    with pytest.raises(ValueError, match="source quote"):
        call_service.create_memecoin_call("bob", "dogecoin")
    with pytest.raises(ValueError, match="source quote"):
        call_service.close_memecoin_call("alice", opened["public_id"])
    assert _rows() == before


def test_close_requires_an_observation_at_or_after_entry(calls_db):
    _refresh(calls_db)
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    entry_at = calls_db["now"]
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, last_updated=(entry_at - timedelta(minutes=1)).isoformat()))

    with pytest.raises(ValueError, match="at or after the entry time"):
        call_service.close_memecoin_call("alice", opened["public_id"])
    assert _rows()[0]["status"] == "active"


def test_retained_fresh_quote_marks_an_active_call(calls_db):
    _refresh(calls_db)
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, "pepe"))

    detail = memecoins.memecoin_detail("dogecoin")
    current = call_service.active_memecoin_call("alice", "dogecoin")

    assert detail["in_current_snapshot"] is False
    assert detail["coin"]["stale"] is False
    assert current["public_id"] == opened["public_id"]
    assert current["mark_price"] == detail["coin"]["price"] == 0.12
    assert current["return_pct"] == 0.0


def test_shared_caller_counts_and_links_use_market_and_coin_identity(calls_db):
    _refresh(calls_db, _coin(calls_db, "first"), _coin(calls_db, "second"))
    first = call_service.create_memecoin_call("alice", "first")
    call_service.create_memecoin_call("alice", "second")
    stock = create_call("alice", "DOGE", entry_price=10, entry_at=calls_db["now"].isoformat())
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, "first", current_price=0.15), _coin(calls_db, "second"))
    call_service.close_memecoin_call("alice", first["public_id"])

    record = web_main._unified_caller_page_data(first["caller_handle"])
    summary = caller_summary_for_user("alice")

    assert first["caller_handle"] == stock["caller_handle"] == summary["handle"]
    assert record["found"] is True
    assert record["stats"] == {
        "total": 3,
        "open": 2,
        "settled": 1,
        "wins": 1,
        "losses": 0,
        "subjects": 3,
    }
    assert summary["total"] == 3 and summary["open"] == 2 and summary["closed"] == 1
    assert summary["wins"] == 1
    assert {item["href"] for item in record["calls"]} == {
        f"{web_main.RUNNERS_ORIGIN}/memecoins/coin/first",
        f"{web_main.RUNNERS_ORIGIN}/memecoins/coin/second",
        f"{web_main.RUNNERS_ORIGIN}/t/DOGE",
    }


@pytest.mark.parametrize("delete_account", [False, True])
def test_export_and_delete_cover_only_the_selected_users_calls(calls_db, delete_account):
    _refresh(calls_db)
    alice = call_service.create_memecoin_call("alice", "dogecoin")
    bob = call_service.create_memecoin_call("bob", "dogecoin")

    exported = export_user_data("alice")

    assert [row["public_id"] for row in exported["memecoin_calls"]] == [alice["public_id"]]
    assert json.loads(exported["memecoin_calls"][0]["entry_evidence"]) == alice["entry_evidence"]
    result = (delete_user_data if delete_account else delete_user_content)("alice")
    assert result["deleted"] is True
    assert [row["public_id"] for row in _rows()] == [bob["public_id"]]
    assert call_service.memecoin_calls(caller_handle=alice["caller_handle"]) == []
    assert memecoins.memecoin_detail("dogecoin")["coin"]["price"] == 0.12
    with connection() as database:
        user = database.execute("SELECT id FROM users WHERE id='alice'").fetchone()
    assert (user is None) == delete_account


def test_api_writes_use_sessions_origin_and_source_prices(calls_db):
    _refresh(calls_db)
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    origin = {"Origin": web_main.RUNNERS_ORIGIN}
    try:
        endpoint = "/api/memecoins/dogecoin/calls"
        assert client.post(endpoint, headers=origin).status_code == 401
        client.cookies.set(web_main.SESSION_COOKIE, "alice-session")
        assert client.post(endpoint).status_code == 403
        assert client.post(endpoint, headers={"Origin": "https://other.example"}).status_code == 403
        response = client.post(
            endpoint,
            headers=origin,
            json={"price": 999, "entry_price": 999, "entry_evidence": {"run_id": "forged"}},
        )
        assert response.status_code == 201
        opened = response.json()["call"]
        assert opened["entry_price"] == 0.12
        assert opened["entry_evidence"]["run_id"] != "forged"
        repeated = client.post(endpoint, headers=origin).json()["call"]
        assert repeated["public_id"] == opened["public_id"]
        my_calls = client.get("/my-calls?market=memecoins", follow_redirects=False)
        assert my_calls.status_code == 303
        assert my_calls.headers["location"] == f"/u/{opened['caller_handle']}?market=memecoins"
        close_url = f"/api/memecoin-calls/{opened['public_id']}/close"
        assert client.post(close_url).status_code == 403
        assert (
            client.post(close_url, headers={"Origin": "https://other.example"}).status_code == 403
        )
        client.cookies.clear()
        assert client.post(close_url, headers=origin).status_code == 401
        client.cookies.set(web_main.SESSION_COOKIE, "bob-session")
        assert client.post(close_url, headers=origin).status_code == 404
        client.cookies.set(web_main.SESSION_COOKIE, "alice-session")
        calls_db["now"] += timedelta(minutes=5)
        _refresh(calls_db, _coin(calls_db, current_price=0.15))
        closed = client.post(close_url, headers=origin, json={"exit_price": 999})
        assert closed.status_code == 200
        assert closed.json()["call"]["exit_price"] == 0.15
        assert client.post(close_url, headers=origin).json() == closed.json()
        public = client.get("/api/memecoin-calls").json()["calls"]
        assert public[0]["public_id"] == opened["public_id"]
        assert "user_id" not in public[0]
        assert client.get("/api/memecoins/dogecoin").json()["calls"][0] == public[0]
        assert client.post("/api/memecoins/unknown/calls", headers=origin).status_code == 404
        calls_db["now"] += timedelta(minutes=16)
        assert client.post(endpoint, headers=origin).status_code == 409
    finally:
        client.close()


@pytest.mark.parametrize("coin_id", ["alpha", "radar"])
def test_navigation_names_are_valid_coin_ids(calls_db, coin_id):
    _refresh(calls_db, _coin(calls_db, coin_id))
    call = call_service.create_memecoin_call("alice", coin_id)
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    try:
        response = client.get(f"/api/memecoins/{coin_id}")
        assert response.status_code == 200
        assert response.json()["coin"]["id"] == coin_id
        assert response.json()["coin"]["detail_url"] == f"/memecoins/coin/{coin_id}"
        assert call["detail_url"] == f"/memecoins/coin/{coin_id}"
    finally:
        client.close()


def test_tiny_prices_survive_quote_history_and_call_receipts(calls_db):
    price = 1.23456789012345e-50
    _refresh(calls_db, _coin(calls_db, current_price=price))
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    detail = memecoins.memecoin_detail("dogecoin")

    assert detail["history"][0]["price"] == price
    assert opened["entry_price"] == price
    assert opened["entry_evidence"]["price"] == price
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, current_price=price * 2))
    closed = call_service.close_memecoin_call("alice", opened["public_id"])
    assert closed["exit_price"] == price * 2
    assert closed["exit_evidence"]["price"] == price * 2
    assert closed["return_pct"] == 100.0


def test_extreme_finite_prices_keep_call_output_valid_json(calls_db):
    _refresh(calls_db, _coin(calls_db, current_price=1e-200))
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, current_price=1e200))

    repeated = call_service.create_memecoin_call("alice", "dogecoin")
    public = call_service.memecoin_calls(coin_id="dogecoin")
    assert repeated["return_pct"] is None
    assert public[0]["return_pct"] is None
    json.dumps({"repeated": repeated, "public": public}, allow_nan=False)
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    try:
        client.cookies.set(web_main.SESSION_COOKIE, "alice-session")
        response = client.post(
            f"/api/memecoin-calls/{opened['public_id']}/close",
            headers={"Origin": web_main.RUNNERS_ORIGIN},
        )
        assert response.status_code == 200
        assert response.json()["call"]["return_pct"] is None
        assert response.json()["call"]["exit_price"] == 1e200
        json.dumps(response.json(), allow_nan=False)
        assert client.get("/api/memecoin-calls").status_code == 200
        record = web_main._unified_caller_page_data(opened["caller_handle"])
        assert record["stats"]["wins"] == caller_summary_for_user("alice")["wins"] == 1
    finally:
        client.close()


@pytest.mark.parametrize("delete_account", [False, True])
def test_account_deletion_clears_the_public_caller_cache(calls_db, delete_account):
    _refresh(calls_db)
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    handle = opened["caller_handle"]
    assert web_main._public_caller_page_data(handle)["stats"]["total"] == 1
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    try:
        client.cookies.set(web_main.SESSION_COOKIE, "alice-session")
        caller_url = f"/u/{handle}?market=memecoins"
        call_link = f'href="{web_main.RUNNERS_ORIGIN}/memecoins/coin/dogecoin"'
        before = client.get(caller_url)
        assert before.status_code == 200
        assert call_link in before.text
        response = client.post(
            "/api/account/delete" if delete_account else "/api/account/data/delete-cloud-copy",
            headers={"Origin": web_main.RUNNERS_ORIGIN},
            json={"confirmation": "DELETE MY ACCOUNT" if delete_account else "MOVE MY DATA"},
        )
        assert response.status_code == 200
        assert _rows() == []
        public = web_main._public_caller_page_data(handle)
        if delete_account:
            assert public == {"found": False}
        else:
            assert public["stats"]["total"] == 0
        after = client.get(caller_url)
        assert after.status_code == (404 if delete_account else 200)
        assert call_link not in after.text
    finally:
        client.close()


def test_public_caller_page_data_reuses_the_cached_build(calls_db, monkeypatch):
    _refresh(calls_db)
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    handle = opened["caller_handle"]

    builds = {"count": 0}
    real_builder = web_main._unified_caller_page_data

    def counting_builder(caller_handle):
        builds["count"] += 1
        return real_builder(caller_handle)

    monkeypatch.setattr(web_main, "_unified_caller_page_data", counting_builder)

    first = web_main._public_caller_page_data(handle)
    assert builds["count"] == 1
    assert first["stats"]["total"] == 1

    second = web_main._public_caller_page_data(handle)
    assert builds["count"] == 1
    assert second == first

    web_main._invalidate_public_screen_data("caller", handle)
    rebuilt = web_main._public_caller_page_data(handle)
    assert builds["count"] == 2
    assert rebuilt["stats"]["total"] == 1


def test_closing_a_winning_memecoin_call_credits_flash(calls_db):
    _refresh(calls_db)
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    assert opened["flash_reward"] == 0
    assert opened["reward_label"] is None
    assert opened["projected_flash_reward"] == 0

    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, current_price=0.15))
    closed = call_service.close_memecoin_call("alice", opened["public_id"])

    assert closed["return_pct"] == 25.0
    assert closed["flash_reward"] == 25
    assert closed["reward_label"] == "+25 Flash"
    with connection() as database:
        credited = [
            dict(row)
            for row in database.execute(
                "SELECT amount,kind,reference_id FROM flash_transactions WHERE user_id='alice'"
            ).fetchall()
        ]
    assert credited == [
        {"amount": 25, "kind": "memecoin_call_win", "reference_id": opened["public_id"]}
    ]

    repeated = call_service.close_memecoin_call("alice", opened["public_id"])
    assert repeated["flash_reward"] == 25
    assert len(_flash_rows("alice")) == 1

    record = web_main._unified_caller_page_data(opened["caller_handle"])
    coin = next(call for call in record["calls"] if call["kind"] == "memecoin")
    assert coin["reward_label"] == "+25 Flash"


def test_closing_a_losing_memecoin_call_earns_nothing(calls_db):
    _refresh(calls_db)
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, current_price=0.06))
    closed = call_service.close_memecoin_call("alice", opened["public_id"])

    assert closed["return_pct"] == -50.0
    assert closed["flash_reward"] == 0
    assert closed["reward_label"] is None
    assert _flash_rows("alice") == []


def test_open_memecoin_call_projection_and_reward_cap(calls_db):
    _refresh(calls_db)
    opened = call_service.create_memecoin_call("alice", "dogecoin")
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, current_price=9.60))

    marked = call_service.memecoin_calls(user_id="alice")[0]
    assert marked["return_pct"] == 7900.0
    assert marked["projected_flash_reward"] == 50
    assert marked["flash_reward"] == 0

    record = web_main._unified_caller_page_data(opened["caller_handle"])
    coin = next(call for call in record["calls"] if call["kind"] == "memecoin")
    assert coin["reward_label"] == "Up to +50 Flash"
