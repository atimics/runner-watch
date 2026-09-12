from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from runner_web import db
from runner_web import telegram_chat as chat
from runner_web.db import connection, init_db

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
CHAT = -1002222222222
ALICE = 4242
BOT_ID = 7849797828
BOT = "DashRATiBot"


@pytest.fixture(autouse=True)
def chat_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "chat.db")
    init_db()


def _update(
    text: str,
    *,
    update_id: int = 1,
    user_id: int | None = ALICE,
    mention: bool = False,
    reply_to_bot: bool = False,
    chat_type: str = "supergroup",
    is_bot: bool = False,
) -> dict:
    entities = []
    body = text
    if mention:
        body = f"@{BOT} {text}"
        entities = [{"type": "mention", "offset": 0, "length": len(BOT) + 1}]
    message: dict = {
        "message_id": 900 + update_id,
        "date": int(NOW.timestamp()),
        "chat": {"id": CHAT, "type": chat_type},
        "from": {"id": user_id, "first_name": "Alice", "is_bot": is_bot},
        "text": body,
    }
    if entities:
        message["entities"] = entities
    if reply_to_bot:
        message["reply_to_message"] = {"from": {"id": BOT_ID, "is_bot": True}}
    return {"update_id": update_id, "message": message}


def _parse(payload: dict):
    return chat.parse_update(payload, bot_username=BOT, bot_id=BOT_ID)


def test_a_plain_group_message_is_read_but_not_addressed():
    message = _parse(_update("anyone watching MSGM"))

    assert message is not None
    assert message.addressed is False
    assert message.user_name == "Alice"
    assert message.chat_id == CHAT


def test_a_mention_and_a_reply_both_count_as_addressing():
    assert _parse(_update("you awake", mention=True)).addressed is True
    assert _parse(_update("and this", reply_to_bot=True)).addressed is True
    assert _parse(_update("and this", reply_to_bot=True)).reply_to_bot is True


def test_tickers_are_picked_out_of_the_text():
    message = _parse(_update("what about $msgm and $VTAK and $msgm again"))

    assert message.tickers == ("MSGM", "VTAK")


def test_a_bare_known_symbol_resolves_like_a_cashtag():
    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) "
            "VALUES(?,?,?,'NASDAQ',?)",
            (1, "MSGM", "Motorsport Games", NOW.isoformat()),
        )
        assert chat.resolve_tickers(database, "what is MSGM doing") == ["MSGM"]
        assert chat.resolve_tickers(database, "$msgm again") == ["MSGM"]


def test_bare_words_that_are_not_known_symbols_are_left_alone():
    with connection() as database:
        assert chat.resolve_tickers(database, "the CEO said IT and DD were fine") == []
        assert chat.resolve_tickers(database, "watching ZZZZ") == []


def test_prefetch_looks_up_a_named_ticker(monkeypatch):
    from runner_web import telegram_chat

    calls: list[str] = []
    monkeypatch.setattr(
        telegram_chat,
        "look_up_ticker",
        lambda symbol: calls.append(symbol) or {"ticker": symbol, "known": True},
    )
    message = _parse(_update("$msgm is moving"))

    with connection() as database:
        grounded = telegram_chat.prefetch_for(message, database)

    assert calls == ["MSGM"]
    assert grounded["looked_up"] == [{"ticker": "MSGM", "known": True}]
    assert "market" not in grounded


def test_prefetch_hands_over_the_board_when_no_ticker_is_named(monkeypatch):
    from runner_web import dash, telegram_chat

    monkeypatch.setattr(dash, "market_now", lambda: {"session": "Pre-market"})
    monkeypatch.setattr(
        dash, "recent_runners", lambda limit=8: {"count": 0, "entries": []}
    )
    message = _parse(_update("whats moving"))

    with connection() as database:
        grounded = telegram_chat.prefetch_for(message, database)

    assert grounded["looked_up"] == []
    assert grounded["market"] == {"session": "Pre-market"}
    assert grounded["recent_runners"] == {"count": 0, "entries": []}


@pytest.mark.parametrize(
    "payload",
    [
        {"update_id": 1},
        {"update_id": 1, "message": {"chat": {"id": 1, "type": "private"}, "text": "hi"}},
        {"update_id": 1, "message": {"chat": {"id": 1, "type": "channel"}, "text": "hi"}},
    ],
)
def test_updates_the_cheetah_should_not_see_are_dropped(payload):
    assert _parse(payload) is None


def test_another_bot_is_ignored():
    assert _parse(_update("beep", is_bot=True)) is None


def test_the_cheetah_ignores_the_room_until_it_is_addressed():
    with connection() as database:
        plain = chat.attention_for(database, _parse(_update("just chatting")), NOW)
    assert plain.consider is False
    assert plain.reason == "not_addressed"


def test_being_addressed_opens_a_run_that_covers_later_messages():
    with connection() as database:
        first = chat.attention_for(database, _parse(_update("hey", mention=True, update_id=1)), NOW)
        assert first.consider is True
        assert first.reason == "addressed"
        chat.open_engagement(database, CHAT, ALICE, NOW)

        later = NOW + timedelta(minutes=1)
        followup = chat.attention_for(database, _parse(_update("and also", update_id=2)), later)

    assert followup.consider is True
    assert followup.reason == "engaged"


def test_a_run_is_spent_after_a_few_replies():
    later = NOW + timedelta(minutes=1)
    with connection() as database:
        chat.open_engagement(database, CHAT, ALICE, NOW)
        for _ in range(chat.ENGAGEMENT_REPLIES):
            chat.spend_engagement(database, CHAT, ALICE, later)
        spent = chat.attention_for(database, _parse(_update("still there", update_id=9)), later)

    assert spent.consider is False
    assert spent.reason == "not_addressed"


def test_a_run_expires_on_its_own():
    with connection() as database:
        chat.open_engagement(database, CHAT, ALICE, NOW)
        stale = NOW + timedelta(minutes=chat.ENGAGEMENT_MINUTES + 1)
        expired = chat.attention_for(database, _parse(_update("hello", update_id=3)), stale)

    assert expired.consider is False


def test_holding_with_stop_mutes_that_person():
    with connection() as database:
        chat.open_engagement(database, CHAT, ALICE, NOW)
        chat.mute_engagement(database, CHAT, ALICE, NOW)
        soon = NOW + timedelta(minutes=1)
        muted = chat.attention_for(database, _parse(_update("oi", mention=True, update_id=4)), soon)

    assert muted.consider is False
    assert muted.reason == "muted"


def test_a_mute_lifts_after_its_window():
    with connection() as database:
        chat.open_engagement(database, CHAT, ALICE, NOW)
        chat.mute_engagement(database, CHAT, ALICE, NOW)
        after = NOW + timedelta(minutes=chat.MUTE_MINUTES + 1)
        lifted = chat.attention_for(
            database, _parse(_update("hi", mention=True, update_id=5)), after
        )

    assert lifted.consider is True


def _log_reply(database, update_id: int, at: datetime) -> None:
    chat.record_action(database, _parse(_update("x", update_id=update_id)), "reply", "hi", at)


def test_a_cooldown_holds_the_cheetah_back_right_after_it_speaks():
    with connection() as database:
        chat.open_engagement(database, CHAT, ALICE, NOW)
        _log_reply(database, 100, NOW)
        straight_after = NOW + timedelta(seconds=max(1, chat.REPLY_COOLDOWN_SECONDS - 5))
        blocked = chat.attention_for(
            database, _parse(_update("again", update_id=6)), straight_after
        )
    assert blocked.consider is False
    assert blocked.reason == "cooldown"


def test_a_direct_mention_outranks_the_cooldown():
    """Two people asking at once must both get a look.

    The cooldown exists to stop the cheetah talking over a room that is not
    talking to him. Applied to a mention it turns into ignoring a question.
    """

    with connection() as database:
        _log_reply(database, 101, NOW)
        straight_after = NOW + timedelta(seconds=1)
        asked = chat.attention_for(
            database, _parse(_update("are you online", mention=True, update_id=7)), straight_after
        )
    assert asked.consider is True
    assert asked.reason == "addressed"


def test_an_hourly_budget_caps_the_cheetah():
    with connection() as database:
        for index in range(chat.REPLIES_PER_HOUR):
            _log_reply(database, 200 + index, NOW - timedelta(minutes=30))
        capped = chat.attention_for(
            database, _parse(_update("more", mention=True, update_id=7)), NOW
        )
    assert capped.consider is False
    assert capped.reason == "hourly_budget"


def test_an_update_is_stored_once_however_often_telegram_retries():
    payload = _update("hello")
    with connection() as database:
        assert chat.record_update(database, payload, NOW) is True
        assert chat.record_update(database, payload, NOW) is False
        stored = database.execute("SELECT COUNT(*) FROM telegram_updates").fetchone()[0]
    assert stored == 1


def test_an_action_is_recorded_once_per_update():
    message = _parse(_update("hello"))
    with connection() as database:
        assert chat.record_action(database, message, "reply", "hi", NOW) is True
        assert chat.record_action(database, message, "reply", "hi", NOW) is False


def test_the_transcript_reads_oldest_first_and_skips_empty_messages():
    with connection() as database:
        for index, text in enumerate(["first", "second", "third"], start=1):
            chat.record_update(database, _update(text, update_id=index), NOW)
        chat.record_update(
            database,
            {"update_id": 50, "message": {"chat": {"id": CHAT, "type": "supergroup"}}},
            NOW,
        )
        database.execute("UPDATE telegram_updates SET status='handled'")
        lines = chat.recent_transcript(database, CHAT)

    assert [line["said"] for line in lines] == ["first", "second", "third"]
    assert {line["who"] for line in lines} == {"Alice"}


def _client():
    from starlette.testclient import TestClient

    from runner_web import main as web_main

    return TestClient(web_main.app, base_url=web_main.APP_ORIGIN)


def test_the_webhook_is_invisible_without_a_configured_secret(monkeypatch):
    from runner_web import main as web_main

    monkeypatch.setattr(web_main, "TELEGRAM_WEBHOOK_SECRET", "")
    client = _client()
    try:
        response = client.post("/telegram/webhook", json=_update("hi"))
    finally:
        client.close()

    assert response.status_code == 404


def test_the_webhook_rejects_a_wrong_or_missing_secret(monkeypatch):
    from runner_web import main as web_main

    monkeypatch.setattr(web_main, "TELEGRAM_WEBHOOK_SECRET", "correct-horse")
    client = _client()
    try:
        missing = client.post("/telegram/webhook", json=_update("hi"))
        wrong = client.post(
            "/telegram/webhook",
            json=_update("hi"),
            headers={"X-Telegram-Bot-Api-Secret-Token": "battery-staple"},
        )
    finally:
        client.close()

    assert missing.status_code == 404
    assert wrong.status_code == 404
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM telegram_updates").fetchone()[0] == 0


def test_the_webhook_stores_an_update_and_a_retry_changes_nothing(monkeypatch):
    from runner_web import main as web_main

    monkeypatch.setattr(web_main, "TELEGRAM_WEBHOOK_SECRET", "correct-horse")
    headers = {"X-Telegram-Bot-Api-Secret-Token": "correct-horse"}
    client = _client()
    try:
        first = client.post("/telegram/webhook", json=_update("hello"), headers=headers)
        retry = client.post("/telegram/webhook", json=_update("hello"), headers=headers)
    finally:
        client.close()

    assert first.status_code == 200
    assert retry.status_code == 200
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM telegram_updates").fetchone()[0] == 1


def test_malformed_bodies_are_refused(monkeypatch):
    from runner_web import main as web_main

    monkeypatch.setattr(web_main, "TELEGRAM_WEBHOOK_SECRET", "correct-horse")
    headers = {"X-Telegram-Bot-Api-Secret-Token": "correct-horse"}
    client = _client()
    try:
        broken = client.post("/telegram/webhook", content=b"{not json", headers=headers)
        listy = client.post("/telegram/webhook", json=[1, 2, 3], headers=headers)
    finally:
        client.close()

    assert broken.status_code == 400
    assert listy.status_code == 400


class _Sent:
    def __init__(self) -> None:
        self.replies: list[tuple[int, str, int | None]] = []
        self.reactions: list[tuple[int, int, str]] = []


@pytest.fixture
def wired(monkeypatch):
    from runner_web import main as web_main

    sent = _Sent()
    monkeypatch.setattr(web_main, "_telegram_identity", lambda: (BOT, BOT_ID))
    monkeypatch.setattr(
        web_main,
        "telegram_config_from_env",
        lambda: __import__("runner_web.telegram", fromlist=["TelegramConfig"]).TelegramConfig(
            bot_token="token", chat_id=str(CHAT)
        ),
    )
    monkeypatch.setattr(
        web_main,
        "send_telegram_reply",
        lambda config, chat_id, text, reply_to_message_id=None: sent.replies.append(
            (chat_id, text, reply_to_message_id)
        ),
    )
    monkeypatch.setattr(
        web_main,
        "set_telegram_reaction",
        lambda config, chat_id, message_id, emoji: sent.reactions.append(
            (chat_id, message_id, emoji)
        ),
    )
    return sent


def test_an_unaddressed_message_costs_nothing_and_says_nothing(wired, monkeypatch):
    from runner_web import main as web_main

    calls: list[Any] = []
    with connection() as database:
        chat.record_update(database, _update("just talking amongst ourselves"), NOW)

    result = web_main.run_telegram_chat(lambda *args: calls.append(args) or {}, at=NOW)

    assert calls == []
    assert result["skipped"] == 1
    assert wired.replies == []


def test_being_addressed_earns_a_reply_and_opens_a_run(wired):
    from runner_web import main as web_main

    with connection() as database:
        chat.record_update(database, _update("what is MSGM doing", mention=True), NOW)

    result = web_main.run_telegram_chat(
        lambda message, transcript: {"action": "reply", "text": "hiss. it is moving"}, at=NOW
    )

    assert result["replied"] == 1
    assert wired.replies == [(CHAT, "hiss. it is moving", 901)]
    with connection() as database:
        run = chat.engagement_for(database, CHAT, ALICE)
    assert run["replies_left"] == chat.ENGAGEMENT_REPLIES - 1


def test_every_mention_in_one_backlog_gets_an_answer(wired):
    """A batch of updates is not one instant.

    Telegram delivers a backlog in a burst, so a tick can carry several
    messages. Judging them all against a single timestamp made each one look
    simultaneous with the reply before it, and the cooldown ate the lot.
    """

    from runner_web import main as web_main

    with connection() as database:
        chat.record_update(database, _update("whats up", mention=True, update_id=1), NOW)
        chat.record_update(database, _update("you there", update_id=2), NOW)
        chat.record_update(database, _update("are you online", mention=True, update_id=3), NOW)

    result = web_main.run_telegram_chat(
        lambda message, transcript: {"action": "reply", "text": f"hiss at {message.update_id}"}
    )

    assert result["replied"] == 2
    assert [text for _, text, _ in wired.replies] == ["hiss at 1", "hiss at 3"]


def test_a_passed_over_message_records_why(wired):
    from runner_web import main as web_main

    with connection() as database:
        chat.record_update(database, _update("nothing to do with the bot"), NOW)

    web_main.run_telegram_chat(lambda *args: {"action": "hold"}, at=NOW)

    with connection() as database:
        row = database.execute(
            "SELECT status,last_error FROM telegram_updates WHERE update_id=1"
        ).fetchone()
    assert row["status"] == "skipped"
    assert row["last_error"] == "not_addressed"


def test_the_cheetah_can_react_instead_of_speaking(wired):
    from runner_web import main as web_main

    with connection() as database:
        chat.record_update(database, _update("look at this", mention=True), NOW)

    result = web_main.run_telegram_chat(
        lambda message, transcript: {"action": "react", "emoji": "👀"}, at=NOW
    )

    assert result["reacted"] == 1
    assert wired.reactions == [(CHAT, 901, "👀")]
    assert wired.replies == []


def test_holding_with_stop_silences_the_cheetah_for_that_person(wired):
    from runner_web import main as web_main

    with connection() as database:
        chat.record_update(database, _update("not now please", mention=True), NOW)

    result = web_main.run_telegram_chat(
        lambda message, transcript: {"action": "hold", "why": "asked to stop", "stop": True},
        at=NOW,
    )

    assert result["held"] == 1
    assert wired.replies == []
    with connection() as database:
        later = NOW + timedelta(minutes=1)
        follow = chat.attention_for(
            database, _parse(_update("hey", mention=True, update_id=2)), later
        )
    assert follow.consider is False
    assert follow.reason == "muted"


def test_a_failed_turn_leaves_the_update_for_another_go(wired):
    from runner_web import main as web_main

    def explode(message, transcript):
        raise RuntimeError("provider down")

    with connection() as database:
        chat.record_update(database, _update("hey", mention=True), NOW)

    web_main.run_telegram_chat(explode, at=NOW)

    with connection() as database:
        row = database.execute("SELECT status,attempts,last_error FROM telegram_updates").fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["last_error"] == "RuntimeError"


def test_without_a_model_the_cheetah_holds(wired):
    from runner_web import main as web_main

    with connection() as database:
        chat.record_update(database, _update("hey", mention=True), NOW)

    result = web_main.run_telegram_chat(None, at=NOW)

    assert result["held"] == 1
    assert wired.replies == []


def _tool_turn(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            }
        ]
    }


def test_the_turn_hands_the_model_what_was_prefetched(monkeypatch):
    from runner_web import main as web_main

    monkeypatch.setattr(
        web_main,
        "telegram_prefetch_for",
        lambda message, database: {
            "resolved_tickers": ["MSGM"],
            "looked_up": [{"ticker": "MSGM", "known": True}],
        },
    )
    calls: list[dict[str, Any]] = []

    def completion(body):
        calls.append(body)
        return _tool_turn("reply", {"text": "MSGM is moving."})

    monkeypatch.setattr(web_main, "_telegram_chat_completion", completion)

    decision = web_main._generate_telegram_turn(_parse(_update("$msgm", mention=True)), [])

    assert decision == {"action": "reply", "text": "MSGM is moving."}
    assert len(calls) == 1
    assert "already_looked_up" in calls[0]["messages"][1]["content"]


def test_a_reply_that_names_an_unlooked_ticker_gets_one_chance_to_correct(monkeypatch):
    from runner_web import main as web_main

    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) "
            "VALUES(?,?,?,'NASDAQ',?)",
            (1, "MSGM", "Motorsport Games", NOW.isoformat()),
        )
    monkeypatch.setattr(
        web_main,
        "telegram_prefetch_for",
        lambda message, database: {"resolved_tickers": [], "looked_up": []},
    )
    scripted = [
        _tool_turn("reply", {"text": "MSGM is up 30%."}),
        _tool_turn("reply", {"text": "I have not looked at MSGM."}, call_id="call-2"),
    ]
    calls: list[dict[str, Any]] = []

    def completion(body):
        calls.append(body)
        return scripted.pop(0)

    monkeypatch.setattr(web_main, "_telegram_chat_completion", completion)

    decision = web_main._generate_telegram_turn(
        _parse(_update("what about that one", mention=True)), []
    )

    assert decision == {"action": "reply", "text": "I have not looked at MSGM."}
    assert len(calls) == 2
