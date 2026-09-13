from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from runner_web import db, main, operations
from runner_web import telegram_outbox as outbox
from runner_web.telegram import TelegramConfig, TelegramDeliveryError, message_units, send_message

AT = datetime(2026, 9, 13, 2, tzinfo=UTC)
CONFIG = TelegramConfig("fixture-token", "fixture-chat")
KINDS = ("runner", "research_report", "stock_filing", "release")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "outbox.db")
    monkeypatch.setenv("TELEGRAM_CHANNEL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("TELEGRAM_TICKER_QUIET_SECONDS", "0")
    db.init_db()


def card(subject="one", *, ticker="AAAA", text=None):
    return {
        "kind": "research_report",
        "subject": subject,
        "ticker": ticker,
        "text": text or f"Research · ${ticker}\nhttps://app.test/research/{subject}",
    }


def queue(cards):
    with db.connection() as database:
        return outbox.enqueue_cards(database, CONFIG.chat_id, cards, at=AT)


def deliver(sender=lambda *_: 42, at=AT, config=CONFIG):
    return outbox.deliver_outbox(config, sender, at=at, kinds=KINDS)


@pytest.mark.parametrize(
    "body,status",
    [
        (b'{"ok":false,"error_code":400}', "failed"),
        (b"{}", "uncertain"),
        (b"invalid", "uncertain"),
        (b"[]", "uncertain"),
        (b'{"ok":true,"result":{"message_id":true}}', "uncertain"),
    ],
)
def test_text_delivery_requires_a_confirmed_receipt(body, status):
    with pytest.raises(TelegramDeliveryError) as error:
        send_message(CONFIG, "Hello", opener=lambda *_args, **_kw: BytesIO(body))
    assert error.value.status == status
    assert CONFIG.bot_token not in str(error.value)


def test_text_rate_limit_retains_delay_and_hides_endpoint():
    def receiver(request, **_):
        raise HTTPError(
            request.full_url, 429, "limited", {}, BytesIO(b'{"parameters":{"retry_after":120}}')
        )

    with pytest.raises(TelegramDeliveryError) as error:
        send_message(CONFIG, "Hello", opener=receiver)
    assert error.value.status == "retry" and error.value.retry_after == 120
    assert CONFIG.bot_token not in str(error.value)


def test_full_cards_survive_unicode_splitting_and_partial_failure():
    cards = [
        card(str(i), text=f"Report {i}\n" + "🦊" * 600 + f"\nhttps://app.test/{i}")
        for i in range(8)
    ]
    queue(cards)
    with db.connection() as database:
        rows = database.execute("SELECT id,text FROM telegram_outbox").fetchall()
        assert len(rows) >= 3
        assert all(message_units(r["text"]) <= 4096 for r in rows)
        assert all(any(c["text"] in r["text"] for r in rows) for c in cards)
    assert deliver()["status"] == "sent"

    def lost(*_):
        raise TimeoutError("lost acknowledgement")

    assert deliver(lost)["status"] == "uncertain"
    with db.connection() as database:
        recorded = database.execute(
            "SELECT status,COUNT(*) FROM telegram_channel_posts GROUP BY status"
        ).fetchall()
        assert {r[0] for r in recorded} == {"sent", "uncertain"}
        assert sum(r[1] for r in recorded) < len(cards)


def test_lost_acknowledgement_and_restart_keep_one_attempt():
    queue([card()])
    sent = []

    def lost(_config, text):
        sent.append(text)
        raise TimeoutError("fixture-token")

    assert deliver(lost)["status"] == "uncertain"
    assert deliver(lost, AT + timedelta(days=1))["status"] == "empty"
    assert len(sent) == 1
    history = outbox.announcement_history()["posts"][0]
    assert history["text"] == sent[0] and history["attempts"] == 1
    assert history["last_error"] == "acknowledgement_lost"


def test_restart_marks_an_interrupted_send_uncertain():
    queue([card()])
    with db.connection() as database:
        database.execute("UPDATE telegram_outbox SET status='sending',attempts=1")
    assert deliver(at=AT + timedelta(minutes=6))["status"] == "empty"
    assert outbox.announcement_history()["posts"][0]["status"] == "uncertain"


def test_retry_uses_frozen_text_and_honors_rate_limit():
    queue([card()])
    sent = []

    def limited(_config, text):
        sent.append(text)
        raise TelegramDeliveryError("retry", retry_after=120)

    assert deliver(limited)["status"] == "retry"
    assert queue([card(text="A changed headline")]) == 0
    assert deliver(at=AT + timedelta(seconds=119))["sent"] == 0
    assert (
        deliver(lambda _c, text: sent.append(text) or 73, AT + timedelta(seconds=121))["sent"] == 1
    )
    assert sent[0] == sent[1]
    post = outbox.announcement_history()["posts"][0]
    assert post["message_id"] == 73 and post["attempts"] == 2


def test_destination_stays_bound_to_queued_message():
    queue([card()])
    assert deliver(config=TelegramConfig("other-token", "other-chat"))["sent"] == 0
    assert queue([card()]) == 0
    assert deliver()["sent"] == 1
    assert outbox.announcement_history()["posts"][0]["chat_id"] == CONFIG.chat_id


def test_channel_budget_and_ticker_quiet_period(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHANNEL_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("TELEGRAM_TICKER_QUIET_SECONDS", "1800")
    monkeypatch.setenv("TELEGRAM_CHANNEL_DAILY_LIMIT", "2")
    queue([card("one")])
    assert deliver()["sent"] == 1
    queue([card("two", ticker="BBBB")])
    assert deliver(at=AT + timedelta(seconds=59))["sent"] == 0
    queue([card("three")])
    assert deliver(at=AT + timedelta(seconds=61))["sent"] == 1
    assert deliver(at=AT + timedelta(hours=1))["sent"] == 0
    assert deliver(at=AT + timedelta(days=1))["sent"] == 1


def _process_send(database_path, ready, finish, results):
    from runner_web import db as child_db

    child_db.DATABASE_PATH = Path(database_path)

    def receiver(*_):
        ready.set()
        finish.wait(10)
        return 42

    results.put(deliver(receiver)["sent"])


def test_separate_processes_claim_a_message_once():
    queue([card()])
    ctx = multiprocessing.get_context("spawn")
    ready, finish, results = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(
        target=_process_send, args=(str(db.DATABASE_PATH), ready, finish, results)
    )
    process.start()
    try:
        assert ready.wait(15)
        assert deliver()["sent"] == 0
    finally:
        finish.set()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0 and results.get(timeout=5) == 1


def test_generated_claims_and_links_stay_out_of_stock_announcements(monkeypatch):
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "fixture")
    monkeypatch.setattr(main, "_telegram_chat_completion", lambda *_: pytest.fail("model invoked"))
    activity = main._activity_payload(
        [{"ticker": "AAAA", "entered_at": AT.isoformat(), "change_pct": 5, "relative_volume": 3}],
        [],
        [],
    )
    text = main._compose_update_announcement(activity)
    assert "$AAAA" in text and "/t/AAAA" in text


def test_sports_card_uses_game_names_and_destinations():
    activity = main._activity_payload(
        [],
        [],
        [
            {
                "ticker": "sports:game-one",
                "public_id": "report",
                "headline": "Game research",
                "away_team_name": "Lions",
                "home_team_name": "Bears",
            }
        ],
    )
    text = main._announcement_cards(activity)[0]["text"]
    assert "Lions at Bears" in text and "$SPORTS" not in text
    assert main.SPORTS_ORIGIN + "/game/game-one" in text
    assert main.SPORTS_ORIGIN + "/research/report" in text


def test_history_requires_operations_access(monkeypatch):
    queue([card()])
    monkeypatch.setattr(operations, "OPERATIONS_TOKEN", "fixture-access")
    client = TestClient(main.app)
    assert client.get("/api/telegram/announcements").status_code == 404
    response = client.get(
        "/api/telegram/announcements", headers={"Authorization": "Bearer fixture-access"}
    )
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json()["posts"][0]["text"] == card()["text"]
    assert client.get("/telegram/announcements").status_code == 200


def test_new_sec_cards_keep_people_actions_and_amendments():
    import json

    from tests.test_stock_map import evidence, filing_row, insert

    insert(filing_row("old", evidence_json="{}"))
    with db.connection() as database:
        db._migration_070_telegram_outbox(database)
    insert(filing_row("new", evidence_json=json.dumps(evidence())))
    insert(filing_row("amended", form="4/A", evidence_json=json.dumps(evidence())))
    with db.connection() as database:
        database.execute(
            "UPDATE sec_filings SET evidence_json=? WHERE accession='old'",
            (json.dumps(evidence()),),
        )
        assert outbox.queue_stock_filings(database, CONFIG, origin="https://app.test", at=AT) == 8
    with db.connection() as database:
        assert outbox.queue_stock_filings(database, CONFIG, origin="https://app.test", at=AT) == 0
        cards = [
            json.loads(r[0])
            for r in database.execute("SELECT card_json FROM telegram_outbox_items").fetchall()
        ]
    assert len(cards) == 8
    purchase = next(c for c in cards if c["subject"] == "new:nonDerivative:1")
    assert "Jane Lee" in purchase["text"] and "Lee Family, LLC" in purchase["text"]
    assert "Shared transaction" in purchase["text"] and "Shares / units: 100" in purchase["text"]
    assert "2026-09-01" in purchase["text"] and "2026-09-05" in purchase["text"]
    assert "https://app.test/t/TEST#ticker-map" in purchase["text"]
    assert "/new/index.htm" in purchase["text"]
    assert {c["event"]["action"] for c in cards} == {
        "Bought",
        "Sold",
        "Award or grant",
        "Exercise or conversion",
    }
    assert all("Amendment" in c["text"] for c in cards if c["subject"].startswith("amended:"))


def test_stake_cards_keep_each_person_and_share_class():
    import json

    from runner_web.stock_map import filing_events
    from tests.test_stock_map import filing_row

    events = filing_events(
        filing_row(
            form="SCHEDULE 13D",
            evidence_json=json.dumps(
                {
                    "positions": [
                        {
                            "name": "Fund",
                            "cik": 101,
                            "shares": 5000,
                            "percent": 7.5,
                            "security": "Class A",
                            "occurred_at": "2026-09-01",
                        },
                        {
                            "name": "Manager",
                            "cik": 102,
                            "shares": 5000,
                            "percent": 7.5,
                            "security": "Class B",
                            "occurred_at": "2026-09-01",
                        },
                    ]
                }
            ),
        )
    )
    cards = [outbox.stock_event_card("TEST", event) for event in events]
    assert "Fund" in cards[0]["text"] and "Class A" in cards[0]["text"]
    assert "Manager" in cards[1]["text"] and "Class B" in cards[1]["text"]
    assert all("Reported stake (%)" in c["text"] for c in cards)
    assert all("Shares / units: 5000" in c["text"] for c in cards)


def test_oversize_cards_fail_before_reserving_any_items():
    with pytest.raises(ValueError, match="announcement_card_size"):
        queue([card("valid"), card("huge", text="🦊" * 2049)])
    with db.connection() as database:
        assert database.execute("SELECT COUNT(*) FROM telegram_outbox_items").fetchone()[0] == 0
