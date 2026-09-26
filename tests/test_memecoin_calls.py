from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from runner_web import db, memecoins
from runner_web import main as web_main
from runner_web import memecoin_calls as call_service
from runner_web.calls import caller_summary_for_user, create_call
from runner_web.db import connection, init_db
from runner_web.flash_wallet import MEMECOIN_CALL_STAKE, credit_flash
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
    with patch.object(
        memecoins,
        "_collect_helius",
        side_effect=lambda **_: (memecoins.normalize_memecoins(json.loads(body)), {}),
    ):
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


def _fund(user_id: str, amount: int = 500) -> None:
    with connection() as database:
        credit_flash(database, user_id, amount, kind="test_grant", reference_id=f"grant-{user_id}")


def _fill(clock: dict[str, datetime], *coins: dict[str, Any], minutes: int = 5) -> list[str]:
    """The next refresh brings a newer quote; pending orders fill at it."""

    clock["now"] += timedelta(minutes=minutes)
    _refresh(clock, *({**coin, "last_updated": clock["now"].isoformat()} for coin in coins))
    return call_service.fill_memecoin_call_orders(clock["now"])


def _find(user_id: str, public_id: str) -> dict[str, Any]:
    return next(
        call
        for call in call_service.memecoin_calls(user_id=user_id, limit=500)
        if call["public_id"] == public_id
    )


def _open(
    clock: dict[str, datetime], user_id: str, coin_id: str = "dogecoin", *coins: dict[str, Any]
) -> dict[str, Any]:
    call_service.create_memecoin_call(user_id, coin_id)
    _fill(clock, *coins)
    call = call_service.active_memecoin_call(user_id, coin_id)
    assert call is not None
    return call


def _close(
    clock: dict[str, datetime], user_id: str, public_id: str, *coins: dict[str, Any]
) -> dict[str, Any]:
    closing = call_service.close_memecoin_call(user_id, public_id)
    assert closing is not None
    _fill(clock, *coins)
    return _find(user_id, public_id)


def _balance(user_id: str) -> int:
    with connection() as database:
        row = database.execute(
            "SELECT balance FROM flash_wallets WHERE user_id=?", (user_id,)
        ).fetchone()
    return int(row["balance"]) if row else 0


def test_a_call_opens_and_closes_at_the_next_quote_with_receipts(calls_db):
    _refresh(calls_db)
    _fund("alice")
    order = call_service.create_memecoin_call("alice", "dogecoin")
    assert order["status"] == "pending" and order["kind"] == "open"
    assert order["stake"] == MEMECOIN_CALL_STAKE
    assert _rows() == []
    # A second request while the first waits is refused.
    with pytest.raises(ValueError, match="waiting for the next quote"):
        call_service.create_memecoin_call("alice", "dogecoin")

    # The shown quote was 0.12; the Call opens at the next one, 0.13.
    fill_run_time = calls_db["now"] + timedelta(minutes=5)
    _fill(calls_db, _coin(calls_db, current_price=0.13))
    opened = call_service.active_memecoin_call("alice", "dogecoin")
    assert opened["entry_price"] == 0.13
    assert opened["entry_at"] == fill_run_time.isoformat()
    assert opened["entry_evidence"]["order_id"] == order["order_id"]
    assert opened["entry_evidence"]["requested_at"] == order["requested_at"]
    assert opened["stake"] == MEMECOIN_CALL_STAKE
    with pytest.raises(ValueError, match="already have an open Call"):
        call_service.create_memecoin_call("alice", "dogecoin")

    closing = call_service.close_memecoin_call("alice", opened["public_id"])
    assert closing["status"] == "active" and closing["closing"] is True
    # Asking again does not queue a second close.
    assert call_service.close_memecoin_call("alice", opened["public_id"])["closing"] is True
    _fill(calls_db, _coin(calls_db, current_price=0.26))
    closed = _find("alice", opened["public_id"])
    assert closed["status"] == "closed"
    assert closed["exit_price"] == 0.26
    assert closed["return_pct"] == 100.0
    assert closed["exit_evidence"]["observed_at"] == closed["exit_at"]
    assert call_service.close_memecoin_call("alice", opened["public_id"]) == closed
    assert len(_rows()) == 1


def test_a_winning_call_returns_its_stake_and_the_win(calls_db):
    _refresh(calls_db)
    _fund("alice", 100)
    opened = _open(calls_db, "alice")
    assert _balance("alice") == 100 - MEMECOIN_CALL_STAKE
    closed = _close(calls_db, "alice", opened["public_id"], _coin(calls_db, current_price=0.15))
    assert closed["return_pct"] == 25.0
    assert closed["flash_reward"] == 25
    assert closed["reward_label"] == "+25 Flash"
    assert _balance("alice") == 125
    kinds = sorted(row["kind"] for row in _flash_rows("alice"))
    assert kinds == ["memecoin_call_settle", "memecoin_call_stake", "test_grant"]


def test_a_losing_call_costs_flash(calls_db):
    _refresh(calls_db)
    _fund("alice", 100)
    opened = _open(calls_db, "alice")
    closed = _close(calls_db, "alice", opened["public_id"], _coin(calls_db, current_price=0.084))
    assert closed["return_pct"] == -30.0
    assert closed["flash_reward"] == -30
    assert closed["reward_label"] == "−30 Flash"
    assert _balance("alice") == 70
    # A total loss costs the whole stake and no more.
    opened = _open(calls_db, "alice")
    _close(calls_db, "alice", opened["public_id"], _coin(calls_db, current_price=0.0001))
    assert _balance("alice") == 70 - MEMECOIN_CALL_STAKE


def test_a_call_needs_flash_for_its_stake(calls_db):
    _refresh(calls_db)
    _fund("alice", MEMECOIN_CALL_STAKE - 1)
    with pytest.raises(ValueError, match="costs"):
        call_service.create_memecoin_call("alice", "dogecoin")
    with connection() as database:
        orders = database.execute("SELECT * FROM memecoin_call_orders").fetchall()
    assert orders == []
    assert _balance("alice") == MEMECOIN_CALL_STAKE - 1


def test_an_order_with_no_new_quote_is_cancelled_and_refunded(calls_db):
    _refresh(calls_db)
    _fund("alice", 100)
    call_service.create_memecoin_call("alice", "dogecoin")
    calls_db["now"] += timedelta(minutes=30)
    assert call_service.fill_memecoin_call_orders(calls_db["now"]) == []
    calls_db["now"] += timedelta(hours=1)
    call_service.fill_memecoin_call_orders(calls_db["now"])
    assert _rows() == []
    assert _balance("alice") == 100
    assert call_service.pending_memecoin_order("alice", "dogecoin") is None


def test_calls_opened_before_staking_keep_the_old_reward(calls_db):
    _refresh(calls_db)
    _fund("alice")
    opened = _open(calls_db, "alice")
    with connection() as database:
        database.execute(
            "UPDATE memecoin_calls SET stake=0 WHERE public_id=?", (opened["public_id"],)
        )
    closed = _close(calls_db, "alice", opened["public_id"], _coin(calls_db, current_price=0.06))
    assert closed["flash_reward"] == 0
    assert closed["reward_label"] is None


def test_coin_ids_and_ownership_keep_calls_separate(calls_db):
    coins = (_coin(calls_db, "first"), _coin(calls_db, "second", current_price=0.3))
    _refresh(calls_db, *coins)
    for user in ("alice", "bob"):
        _fund(user)
    call_service.create_memecoin_call("alice", "first")
    call_service.create_memecoin_call("alice", "second")
    call_service.create_memecoin_call("bob", "first")
    _fill(calls_db, *coins)
    first = call_service.active_memecoin_call("alice", "first")
    second = call_service.active_memecoin_call("alice", "second")
    other_owner = call_service.active_memecoin_call("bob", "first")

    assert len({item["public_id"] for item in (first, second, other_owner)}) == 3
    assert first["entry_price"] == 0.12 and second["entry_price"] == 0.3
    assert first["detail_url"] == "/memecoins/coin/first"
    assert first["caller_handle"] == second["caller_handle"]
    assert first["caller_handle"] != other_owner["caller_handle"]
    assert call_service.close_memecoin_call("bob", first["public_id"]) is None
    assert len(call_service.memecoin_calls(coin_id="first")) == 2


@pytest.mark.parametrize("state", ["stale", "future", "disabled"])
def test_opening_requires_a_fresh_enabled_source(calls_db, monkeypatch, state):
    _refresh(calls_db)
    _fund("bob")
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
    assert _balance("bob") == 500


def test_a_close_fills_only_at_a_quote_after_the_request(calls_db):
    _refresh(calls_db)
    _fund("alice")
    opened = _open(calls_db, "alice")
    call_service.close_memecoin_call("alice", opened["public_id"])
    # A refresh that carries no newer observation fills nothing.
    call_service.fill_memecoin_call_orders(calls_db["now"])
    assert _find("alice", opened["public_id"])["status"] == "active"
    _fill(calls_db, _coin(calls_db, current_price=0.15))
    assert _find("alice", opened["public_id"])["exit_price"] == 0.15


def test_retained_fresh_quote_marks_an_active_call(calls_db):
    _refresh(calls_db)
    _fund("alice")
    opened = _open(calls_db, "alice")
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, "pepe"))

    detail = memecoins.memecoin_detail("dogecoin")
    current = call_service.active_memecoin_call("alice", "dogecoin")
    assert detail["in_current_snapshot"] is False
    assert current["public_id"] == opened["public_id"]
    assert current["mark_price"] == detail["coin"]["price"] == 0.12
    assert current["return_pct"] == 0.0


def test_shared_caller_counts_and_links_use_market_and_coin_identity(calls_db):
    coins = (_coin(calls_db, "first"), _coin(calls_db, "second"))
    _refresh(calls_db, *coins)
    _fund("alice")
    call_service.create_memecoin_call("alice", "first")
    call_service.create_memecoin_call("alice", "second")
    _fill(calls_db, *coins)
    first = call_service.active_memecoin_call("alice", "first")
    stock = create_call("alice", "DOGE", entry_price=10, entry_at=calls_db["now"].isoformat())
    _close(
        calls_db,
        "alice",
        first["public_id"],
        _coin(calls_db, "first", current_price=0.15),
        _coin(calls_db, "second"),
    )

    record = web_main._unified_caller_page_data(first["caller_handle"])
    summary = caller_summary_for_user("alice")
    assert first["caller_handle"] == stock["caller_handle"] == summary["handle"]
    assert record["stats"] == {
        "total": 3,
        "open": 2,
        "settled": 1,
        "wins": 1,
        "losses": 0,
        "subjects": 3,
    }
    assert {item["href"] for item in record["calls"]} == {
        f"{web_main.RUNNERS_ORIGIN}/memecoins/coin/first",
        f"{web_main.RUNNERS_ORIGIN}/memecoins/coin/second",
        f"{web_main.RUNNERS_ORIGIN}/stock/DOGE",
    }


@pytest.mark.parametrize("delete_account", [False, True])
def test_export_and_delete_cover_only_the_selected_users_calls(calls_db, delete_account):
    _refresh(calls_db)
    for user in ("alice", "bob"):
        _fund(user)
    alice = _open(calls_db, "alice")
    bob = _open(calls_db, "bob")
    call_service.close_memecoin_call("alice", alice["public_id"])

    exported = export_user_data("alice")
    assert [row["public_id"] for row in exported["memecoin_calls"]] == [alice["public_id"]]
    assert [row["kind"] for row in exported["memecoin_call_orders"]] == ["open", "close"]
    result = (delete_user_data if delete_account else delete_user_content)("alice")
    assert result["deleted"] is True
    assert [row["public_id"] for row in _rows()] == [bob["public_id"]]
    with connection() as database:
        left = database.execute("SELECT user_id FROM memecoin_call_orders").fetchall()
    assert {row["user_id"] for row in left} == {"bob"}


def test_api_writes_queue_orders_behind_sessions_and_origin(calls_db):
    _refresh(calls_db)
    _fund("alice")
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
        assert response.status_code == 202
        assert response.json()["order"]["status"] == "pending"
        assert response.json()["balance"] == 500 - MEMECOIN_CALL_STAKE
        assert client.post(endpoint, headers=origin).status_code == 409
        _fill(calls_db)
        opened = call_service.active_memecoin_call("alice", "dogecoin")
        assert opened["entry_evidence"]["run_id"] != "forged"
        close_url = f"/api/memecoin-calls/{opened['public_id']}/close"
        assert client.post(close_url).status_code == 403
        client.cookies.set(web_main.SESSION_COOKIE, "bob-session")
        assert client.post(close_url, headers=origin).status_code == 404
        client.cookies.set(web_main.SESSION_COOKIE, "alice-session")
        closing = client.post(close_url, headers=origin, json={"exit_price": 999})
        assert closing.status_code == 200
        assert closing.json()["call"]["closing"] is True
        _fill(calls_db, _coin(calls_db, current_price=0.15))
        closed = client.post(close_url, headers=origin).json()["call"]
        assert closed["exit_price"] == 0.15
        public = client.get("/api/memecoin-calls").json()["calls"]
        assert public[0]["public_id"] == opened["public_id"]
        assert "user_id" not in public[0]
        assert client.post("/api/memecoins/unknown/calls", headers=origin).status_code == 404
    finally:
        client.close()


@pytest.mark.parametrize("coin_id", ["alpha", "radar"])
def test_navigation_names_are_valid_coin_ids(calls_db, coin_id):
    _refresh(calls_db, _coin(calls_db, coin_id))
    _fund("alice")
    call = _open(calls_db, "alice", coin_id, _coin(calls_db, coin_id))
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    try:
        response = client.get(f"/api/memecoins/{coin_id}")
        assert response.status_code == 200
        assert response.json()["coin"]["id"] == coin_id
        assert call["detail_url"] == f"/memecoins/coin/{coin_id}"
    finally:
        client.close()


def test_tiny_prices_survive_quote_history_and_call_receipts(calls_db):
    price = 1.23456789012345e-50
    _refresh(calls_db, _coin(calls_db, current_price=price))
    _fund("alice")
    opened = _open(calls_db, "alice", "dogecoin", _coin(calls_db, current_price=price))
    assert opened["entry_price"] == price
    assert opened["entry_evidence"]["price"] == price
    closed = _close(
        calls_db, "alice", opened["public_id"], _coin(calls_db, current_price=price * 2)
    )
    assert closed["exit_price"] == price * 2
    assert closed["return_pct"] == 100.0


def test_extreme_finite_prices_keep_call_output_valid_json(calls_db):
    _refresh(calls_db, _coin(calls_db, current_price=1e-200))
    _fund("alice")
    opened = _open(calls_db, "alice", "dogecoin", _coin(calls_db, current_price=1e-200))
    closed = _close(calls_db, "alice", opened["public_id"], _coin(calls_db, current_price=1e200))
    public = call_service.memecoin_calls(coin_id="dogecoin")
    assert closed["return_pct"] is None
    assert public[0]["return_pct"] is None
    json.dumps({"closed": closed, "public": public}, allow_nan=False)
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    try:
        assert client.get("/api/memecoin-calls").status_code == 200
    finally:
        client.close()


@pytest.mark.parametrize("delete_account", [False, True])
def test_account_deletion_clears_the_public_caller_cache(calls_db, delete_account):
    _refresh(calls_db)
    _fund("alice")
    opened = _open(calls_db, "alice")
    handle = opened["caller_handle"]
    assert web_main._public_caller_page_data(handle)["stats"]["total"] == 1
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    try:
        client.cookies.set(web_main.SESSION_COOKIE, "alice-session")
        caller_url = f"/u/{handle}?market=memecoins"
        call_link = f'href="{web_main.RUNNERS_ORIGIN}/memecoins/coin/dogecoin"'
        assert call_link in client.get(caller_url).text
        response = client.post(
            "/api/account/delete" if delete_account else "/api/account/data/delete-cloud-copy",
            headers={"Origin": web_main.RUNNERS_ORIGIN},
            json={"confirmation": "DELETE MY ACCOUNT" if delete_account else "MOVE MY DATA"},
        )
        assert response.status_code == 200
        assert _rows() == []
        after = client.get(caller_url)
        assert after.status_code == (404 if delete_account else 200)
        assert call_link not in after.text
    finally:
        client.close()


def test_public_caller_page_data_reuses_the_cached_build(calls_db, monkeypatch):
    _refresh(calls_db)
    _fund("alice")
    opened = _open(calls_db, "alice")
    handle = opened["caller_handle"]
    builds = {"count": 0}
    real_builder = web_main._unified_caller_page_data

    def counting_builder(caller_handle):
        builds["count"] += 1
        return real_builder(caller_handle)

    monkeypatch.setattr(web_main, "_unified_caller_page_data", counting_builder)
    first = web_main._public_caller_page_data(handle)
    second = web_main._public_caller_page_data(handle)
    assert builds["count"] == 1 and second == first
    web_main._invalidate_public_screen_data("caller", handle)
    web_main._public_caller_page_data(handle)
    assert builds["count"] == 2


def test_open_call_projection_shows_the_signed_result(calls_db):
    _refresh(calls_db)
    _fund("alice")
    opened = _open(calls_db, "alice")
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, current_price=9.60))
    marked = call_service.memecoin_calls(user_id="alice")[0]
    assert marked["projected_flash_reward"] == 50
    record = web_main._unified_caller_page_data(opened["caller_handle"])
    coin = next(call for call in record["calls"] if call["kind"] == "memecoin")
    assert coin["reward_label"] == "+50 Flash at this price"
    calls_db["now"] += timedelta(minutes=5)
    _refresh(calls_db, _coin(calls_db, current_price=0.06))
    record = web_main._unified_caller_page_data(opened["caller_handle"])
    coin = next(call for call in record["calls"] if call["kind"] == "memecoin")
    assert coin["reward_label"] == "−50 Flash at this price"


@pytest.mark.parametrize("market", ["stocks", "memecoins"])
def test_own_call_record_survives_close_and_reload(calls_db, monkeypatch, market):
    _refresh(calls_db)
    if market == "stocks":
        subject, page = "RUN", "/stock/RUN"
        create_url = "/api/calls/stock/RUN"
        close_prefix = "/api/calls/stock/"
        current = {"price": 1.5, "quote_time": calls_db["now"].isoformat()}
        data = {"ticker": "RUN", "company": "Runner", "current": current, "can_publish": True}
        monkeypatch.setattr(web_main, "_known_ticker", lambda *a: True)
        monkeypatch.setattr(web_main, "ticker_detail_data", lambda *a: data)
        monkeypatch.setattr(web_main, "_public_ticker_detail_data", lambda *a: data)
        monkeypatch.setattr(web_main, "ticker_quote", lambda *a, **k: {})
        monkeypatch.setattr(
            web_main,
            "market_mark",
            lambda *a, **kw: {
                "price": current["price"],
                "observed_at": current["quote_time"],
                "source": "test",
                "age_seconds": 0,
                "session": "regular",
            },
        )
        monkeypatch.setattr(web_main, "ticker_chart_detail_payload", lambda *a: {"points": []})
    else:
        subject, page = "dogecoin", "/memecoins/coin/dogecoin"
        create_url = "/api/memecoins/dogecoin/calls"
        close_prefix = "/api/memecoin-calls/"
    monkeypatch.setattr(web_main, "enforce_rate", lambda *a, **kw: None)
    _fund("alice")
    client = TestClient(web_main.app, base_url=web_main.RUNNERS_ORIGIN)
    client.cookies.set(web_main.SESSION_COOKIE, "alice-session")
    headers = {"Origin": web_main.RUNNERS_ORIGIN}
    endpoint = f"/api/screens/{market}/{subject}/detail"
    try:
        preview = client.get(endpoint).json()
        assert preview["call"]["status"] == "none"
        action = preview["actions"][0]
        assert "rise" in action["preview"]
        response = client.post(create_url, json=action["body"], headers=headers)
        if market == "memecoins":
            assert response.status_code == 202
            assert "next quote" in action["preview"]
            waiting = client.get(endpoint).json()
            assert waiting["actions"] == []
            assert {"label": "Your Call", "value": "Opening at the next quote"} in waiting["facts"]
            _fill(calls_db)
            call_id = call_service.active_memecoin_call("alice", subject)["public_id"]
        else:
            assert response.status_code == 201
            call_id = response.json()["call"]["public_id"]
        for _ in range(2):
            record = client.get(endpoint).json()["call"]
            assert record["status"] == "active"
            assert record["choice"].endswith("rises")
            assert "$1.50" in record["entry"] if market == "stocks" else "$0.12" in record["entry"]
            assert "Settles after" in record["terms"]
            html = client.get(page)
            assert html.status_code == 200
            assert record["entry"] in html.text
        calls_db["now"] += timedelta(minutes=6)
        if market == "stocks":
            current.update(price=3, quote_time=calls_db["now"].isoformat())
            # The stock freshness clock follows the controlled quote.
            monkeypatch.setattr(web_main, "now", lambda: calls_db["now"])
        else:
            _refresh(calls_db, _coin(calls_db, current_price=0.24))
        preview = client.get(endpoint).json()
        assert preview["call"]["return"] == "+100.0%"
        assert preview["actions"][0]["preview"].startswith("Close your Call")
        closed = client.post(
            close_prefix + call_id + "/close", json=preview["actions"][0]["body"], headers=headers
        )
        assert closed.status_code == 200
        if market == "memecoins":
            _fill(calls_db, _coin(calls_db, current_price=0.24))
            earned = _find("alice", call_id)["flash_reward"]
        else:
            earned = closed.json()["reward"]
        assert earned > 0
        count = len(_flash_rows("alice"))
        for _ in range(2):
            record = client.get(endpoint).json()["call"]
            assert record["status"] == "closed"
            assert "Closed at" in record["outcome"]
            assert record["return"] == "+100.0%"
            assert record["reward"] == f"{earned} Flash"
            html = client.get(page)
            assert record["reward"] in html.text
        assert len(_flash_rows("alice")) == count
        client.cookies.set(web_main.SESSION_COOKIE, "bob-session")
        assert client.get(endpoint).json()["call"]["status"] == "none"
    finally:
        client.close()
