from __future__ import annotations

import json
from datetime import timedelta

import pytest

from runner_web import dash, dash_intake, db, memecoins, telegram_chat
from runner_web.db import connection, init_db
from runner_web.helius_discovery import _encode
from runner_web.market_actors import coin_subject_key
from tests.test_telegram_chat import BOT, BOT_ID, CA, CHAT, NOW, _parse, _update


@pytest.fixture(autouse=True)
def intake_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "intake.db")
    monkeypatch.setenv("MEMECOINS_ENABLED", "true")
    init_db()


def _queue():
    with connection() as database:
        row = database.execute(
            "SELECT value FROM worker_state WHERE key='memecoin_searched'"
        ).fetchone()
    return json.loads(row[0]) if row else {}


def _forward(text=CA, *, update_id=1):
    payload = _update(text, chat_type="private", update_id=update_id)
    payload["message"]["forward_origin"] = {
        "type": "channel",
        "chat": {"id": -10042, "title": "Mad Apes"},
        "message_id": 27,
        "date": int(NOW.timestamp()),
    }
    return payload


@pytest.mark.parametrize("caption", [False, True])
def test_forwarded_ca_is_addressed_and_keeps_telegram_origin(caption):
    payload = _forward()
    if caption:
        payload["message"]["caption"] = payload["message"].pop("text")
    message = _parse(payload)
    assert message.addressed
    assert message.addresses == (CA,)
    assert message.source["title"] == "Mad Apes"
    assert message.source["message_id"] == 27


def test_copied_forward_works_with_its_available_source():
    message = _parse(_update(f"CA: {CA}", chat_type="private"))
    assert message.addressed
    assert message.addresses == (CA,)
    assert message.source["kind"] == "submitted"


def test_emoji_and_leading_spaces_preserve_telegram_mention_offsets():
    payload = _update(f"  🐆 @{BOT} {CA}")
    payload["message"]["entities"] = [{"type": "mention", "offset": 5, "length": len(BOT) + 1}]
    assert _parse(payload).addressed


@pytest.mark.parametrize("link", [f"https://pump.fun/coin/{CA}", f"https://solscan.io/token/{CA}"])
def test_visible_and_hidden_token_links_resolve(link):
    assert dash_intake.extract_addresses(link) == (CA,)
    assert dash_intake.extract_addresses("Chart", [{"type": "text_link", "url": link}]) == (CA,)


@pytest.mark.parametrize(
    "text",
    [
        "z" * 44,
        "0" + CA,
        CA + "0",
        "x" * 64,
        f"https://dexscreener.com/solana/{CA}",
        f"https://solscan.io/account/{CA}",
        f"https://example.com/{CA}",
    ],
)
def test_other_identifiers_stay_out_of_token_intake(text):
    assert dash_intake.extract_addresses(text) == ()


def test_duplicate_addresses_keep_their_case_and_one_entry():
    assert dash_intake.extract_addresses(f"{CA}\nCA: {CA}") == (CA,)


def test_forwarder_bots_require_configured_sender_id(monkeypatch):
    payload = _update(CA, is_bot=True, user_id=123, chat_type="private")
    assert _parse(payload) is None
    monkeypatch.setenv("TELEGRAM_FORWARDER_BOT_IDS", "123")
    assert _parse(payload).addresses == (CA,)
    assert _parse(_update("hello", is_bot=True, user_id=123)) is None
    monkeypatch.setenv("TELEGRAM_FORWARDER_BOT_IDS", str(BOT_ID))
    assert _parse(_update(CA, is_bot=True, user_id=BOT_ID)) is None


def test_dm_never_becomes_the_proactive_group_destination(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ROOM_CHAT_ID", raising=False)
    private = _forward(update_id=2)
    private["message"]["chat"]["id"] = 4242
    with connection() as database:
        telegram_chat.record_update(database, _update("hello"), NOW)
        telegram_chat.record_update(database, private, NOW)
    assert telegram_chat.room_chat_id() == CHAT


def test_private_chat_alone_keeps_proactive_group_destination_empty(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ROOM_CHAT_ID", raising=False)
    private = _forward()
    private["message"]["chat"]["id"] = 4242
    with connection() as database:
        telegram_chat.record_update(database, private, NOW)
    assert telegram_chat.room_chat_id() is None


def test_forward_and_website_search_share_one_queue_and_original_request_time():
    message = _parse(_forward())
    loaded = dash_intake.ingest_message(message, at=NOW)
    assert loaded.coin_lookups[0]["requested"]
    assert _queue() == {CA: NOW.isoformat()}
    assert memecoins.request_memecoin(CA, at=NOW + timedelta(minutes=1))
    dash_intake.ingest_message(message, at=NOW + timedelta(minutes=2))
    assert _queue() == {CA: NOW.isoformat()}


@pytest.mark.parametrize("forwarded", [True, False])
def test_worker_queues_a_submission_and_replies_without_a_model(monkeypatch, forwarded):
    from runner_web import main
    from runner_web.telegram import TelegramConfig

    sent = []
    monkeypatch.setattr(main, "_telegram_identity", lambda: (BOT, BOT_ID))
    monkeypatch.setattr(
        main, "telegram_config_from_env", lambda: TelegramConfig("token", str(CHAT))
    )
    monkeypatch.setattr(
        main, "send_telegram_reply", lambda *args, **kwargs: sent.append((args, kwargs))
    )
    payload = _forward() if forwarded else _update(CA, chat_type="private")
    with connection() as database:
        assert telegram_chat.record_update(database, payload, NOW)
        assert not telegram_chat.record_update(database, payload, NOW)
    main.run_telegram_chat(lambda *_: pytest.fail("forwarded CA used model"), at=NOW)
    main.run_telegram_chat(at=NOW)
    assert _queue() == {CA: NOW.isoformat()}
    assert len(sent) == 1
    assert "assessment queue" in sent[0][0][2]
    assert sent[0][1]["preview_url"].endswith("/memecoins?q=" + CA)


def test_ca_in_group_enters_queue_during_quiet_attention(monkeypatch):
    from runner_web import main
    from runner_web.telegram import TelegramConfig

    monkeypatch.setattr(main, "_telegram_identity", lambda: (BOT, BOT_ID))
    monkeypatch.setattr(
        main, "telegram_config_from_env", lambda: TelegramConfig("token", str(CHAT))
    )
    with connection() as database:
        telegram_chat.record_update(database, _update(CA), NOW)
    main.run_telegram_chat(lambda *_: pytest.fail("quiet group used model"), at=NOW)
    assert CA in _queue()


def test_burst_limit_is_reported_and_shared_lookup_stays_bounded():
    addresses = [_encode(bytes([i]) * 32) for i in range(1, 7)]
    for address in addresses[:5]:
        assert memecoins.request_memecoin(address, at=NOW, source="dash")
    message = _parse(_forward(addresses[5]))
    loaded = dash_intake.ingest_message(message, at=NOW)
    assert not loaded.coin_lookups[0].get("requested")
    assert "Try again later" in dash_intake.intake_reply(loaded)["text"]
    assert len(_queue()) == 5


def test_prefetch_uses_the_website_assessment_already_loaded(monkeypatch):
    from dataclasses import replace

    assessment = {"known": True, "contract_address": CA, "assessment": {"state": "partial"}}
    message = replace(_parse(_forward()), coin_lookups=(assessment,))
    monkeypatch.setattr(dash, "coin_detail", lambda *_: pytest.fail("repeated lookup"))
    with connection() as database:
        grounded = telegram_chat.prefetch_for(message, database)
    assert grounded["looked_up_coins"] == [assessment]
    assert "market" not in grounded


def test_saved_coin_outside_board_gets_shared_assessment_and_refresh(monkeypatch):
    rules = {"ratified": False, "met": 3, "total": 9}
    receipt = {"state": "partial", "risk": {"factors": [{"title": "Creator selling"}]}}
    coin = {
        "id": coin_subject_key(CA),
        "token_address": CA,
        "symbol": "TEST",
        "price": 0.01,
        "price_label": "$0.0100",
        "stale": False,
        "attention_score": 47,
        "ratification": rules,
        "memecoin_assessment": receipt,
        "risks": ["Creator selling"],
    }
    monkeypatch.setattr(memecoins, "memecoin_market", lambda **_: {"rows": []})
    looked = []
    monkeypatch.setattr(
        memecoins,
        "memecoin_detail",
        lambda key, **_: (
            looked.append(key)
            or {
                "coin": coin,
                "status": "ok",
                "history": [],
            }
        ),
    )
    result = dash.coin_detail(CA, at=NOW)
    assert looked == [coin_subject_key(CA)]
    assert result["assessment"] == receipt
    assert result["ratification"] == rules
    assert result["attention_score"] == 47
    assert result["risks"] == ["Creator selling"]
    assert result["requested"]


def test_stale_website_data_is_labelled_in_receipt():
    from dataclasses import replace

    message = replace(
        _parse(_forward()),
        coin_lookups=(
            {
                "known": True,
                "contract_address": CA,
                "stale": True,
                "price": "$1",
                "ratification": {"ratified": True},
                "attention_score": 90,
            },
        ),
    )
    text = dash_intake.intake_reply(message)["text"]
    assert "waiting for fresh data" in text
    assert "Ratified" not in text and "$1" not in text


def test_forward_is_picked_up_by_normal_search_worker_and_matches_website(monkeypatch):
    from runner_web.memecoin_model import assess_memecoin

    dash_intake.ingest_message(_parse(_forward()), at=NOW)
    pool = _encode(bytes([42]) * 32)
    downloaded = []

    def download(url, timeout):
        downloaded.append(url)
        return json.dumps(
            {
                "data": [
                    {
                        "id": "solana_" + CA,
                        "relationships": {"top_pools": {"data": [{"id": "solana_" + pool}]}},
                    }
                ],
                "included": [
                    {
                        "id": "solana_" + pool,
                        "type": "pool",
                        "attributes": {"reserve_in_usd": "9000"},
                    }
                ],
            }
        ).encode()

    pools, fetched = memecoins._searched_pools(set(), download=download, at=NOW)
    assert fetched and pools[0]["token_address"] == CA
    assert CA in downloaded[0]
    row = memecoins.normalize_memecoins(
        [
            {
                "id": coin_subject_key(CA),
                "symbol": "TEST",
                "name": "Test",
                "current_price": 0.01,
                "total_volume": 10000,
                "market_cap": 90000,
                "price_change_percentage_24h": 30,
                "last_updated": NOW.isoformat(),
            }
        ]
    )[0]
    row.update(token_address=CA, network="solana", liquidity_usd=9000)
    row.update(assess_memecoin(row, events=[], findings=[], coverage={}, at=NOW))
    row["ratification"] = {"ratified": False, "met": 3, "total": 9}
    monkeypatch.setattr(memecoins, "_collect_helius", lambda **_: ([row], {}))
    memecoins.refresh_memecoins(download=lambda *_: b"", at=NOW)
    board = memecoins.memecoin_market(query=CA, at=NOW)["rows"][0]
    detail = memecoins.memecoin_detail(board["id"], at=NOW)["coin"]
    dash_coin = dash.coin_detail(CA, at=NOW)
    assert dash_coin["known"] and not dash_coin["requested"]
    assert dash_coin["assessment"] == detail["memecoin_assessment"]
    assert dash_coin["ratification"] == detail["ratification"]
    assert dash_coin["attention_score"] == board["attention_score"]
    assert dash_coin["price"] == board["price_label"]
