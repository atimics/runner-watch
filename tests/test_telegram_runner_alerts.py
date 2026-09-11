from __future__ import annotations

import json
import urllib.error

import pytest
from pytest import MonkeyPatch

from runner_web import db, telegram
from runner_web import main as web_main
from runner_web.db import connection, init_db


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"ok":true}') -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _insert_runner(
    scan_run_id: str,
    ticker: str,
    *,
    score: float,
    entered_at: str,
    price: float = 10.0,
    change_pct: float = 5.0,
    relative_volume: float = 3.0,
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_runs(
                id,mode,label,feature_schema_version,requested_symbols,liquid_symbols,
                scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                started_at,finished_at,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                scan_run_id,
                "penny",
                "Penny runners",
                "test",
                1,
                1,
                1,
                1,
                "[]",
                "[]",
                entered_at,
                entered_at,
                entered_at,
            ),
        )
        snapshot_id = f"{scan_run_id}:{ticker}"
        database.execute(
            """
            INSERT INTO scan_snapshots(
                id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                momentum_15m_pct,relative_volume,breakout_pct,dollar_volume,quote_time,
                signals_json,risks_json,captured_at,scan_run_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                ticker,
                score,
                "watching",
                "regular",
                price,
                change_pct,
                1.0,
                2.0,
                relative_volume,
                0.0,
                1_000_000.0,
                entered_at,
                "[]",
                "[]",
                entered_at,
                scan_run_id,
            ),
        )
        database.execute(
            """
            INSERT INTO pulse_entries(ticker,entered_at,scan_run_id,snapshot_id,price,created_at)
            VALUES(?,?,?,?,?,?)
            """,
            (ticker, entered_at, scan_run_id, snapshot_id, price, entered_at),
        )


@pytest.fixture
def alert_environment(tmp_path, monkeypatch: MonkeyPatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "telegram-alerts.db")
    monkeypatch.setenv("TELEGRAM_RUNNER_ALERTS", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setenv("TELEGRAM_MIN_SCORE", "60")
    monkeypatch.setenv("TELEGRAM_MAX_PER_RUN", "10")
    init_db()
    return tmp_path


def test_alerts_default_to_off(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_RUNNER_ALERTS", raising=False)

    assert telegram.alerts_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_alerts_parse_truthy_values(monkeypatch: MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("TELEGRAM_RUNNER_ALERTS", value)

    assert telegram.alerts_enabled() is True


def test_config_reads_environment(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " token ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " chat ")
    monkeypatch.setenv("TELEGRAM_MIN_SCORE", "72.5")
    monkeypatch.setenv("TELEGRAM_MAX_PER_RUN", "4")

    config = telegram.config_from_env()

    assert config.bot_token == "token"
    assert config.chat_id == "chat"
    assert config.min_score == 72.5
    assert config.max_per_run == 4
    assert config.configured is True


def test_config_ignores_invalid_numbers(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_MIN_SCORE", "not-a-number")
    monkeypatch.setenv("TELEGRAM_MAX_PER_RUN", "-3")

    config = telegram.config_from_env()

    assert config.min_score == telegram.DEFAULT_MIN_SCORE
    assert config.max_per_run == 1


def test_select_new_runners_filters_orders_and_caps() -> None:
    entries = [
        {"ticker": "LOW", "score": 40},
        {"ticker": "HIGH", "score": 90},
        {"ticker": "MID", "score": 70},
        {"ticker": "NONE"},
    ]

    selected = telegram.select_new_runners(entries, min_score=60, limit=10)

    assert [entry["ticker"] for entry in selected] == ["HIGH", "MID"]
    assert telegram.select_new_runners(entries, min_score=0, limit=1)[0]["ticker"] == "HIGH"


def test_format_runner_digest_includes_metrics_and_link() -> None:
    text = telegram.format_runner_digest(
        [
            {
                "ticker": "abcd",
                "price": 12.34,
                "change_pct": 18.4,
                "relative_volume": 5.2,
                "score": 78,
            }
        ],
        origin="https://runners.rati.chat",
    )

    assert "1 new runner detected" in text
    assert "$ABCD" in text
    assert "$12.34" in text
    assert "+18.40%" in text
    assert "RVOL 5.2x" in text
    assert "score 78" in text
    assert "https://runners.rati.chat/t/ABCD" in text


def test_format_runner_digest_counts_several_runners() -> None:
    text = telegram.format_runner_digest(
        [{"ticker": "AAAA", "score": 70}, {"ticker": "BBBB", "score": 65}],
        origin="https://runners.rati.chat/",
    )

    assert "2 new runners detected" in text
    assert "https://runners.rati.chat/t/AAAA" in text
    assert "https://runners.rati.chat/t/BBBB" in text


def test_send_message_requires_configuration() -> None:
    with pytest.raises(RuntimeError):
        telegram.send_message(telegram.TelegramConfig(bot_token="", chat_id=""), "hello")


def test_send_message_posts_payload_to_bot_endpoint() -> None:
    captured: dict[str, object] = {}

    def opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _FakeResponse()

    telegram.send_message(
        telegram.TelegramConfig(bot_token="tok", chat_id="123"),
        "hello",
        opener=opener,
    )

    assert captured["url"] == "https://api.telegram.org/bottok/sendMessage"
    assert captured["body"] == {"chat_id": "123", "text": "hello", "disable_notification": False}


def test_send_message_rejects_an_api_error() -> None:
    def opener(request, timeout=None):
        return _FakeResponse(status=400, body=b'{"ok":false}')

    with pytest.raises(urllib.error.HTTPError):
        telegram.send_message(
            telegram.TelegramConfig(bot_token="tok", chat_id="123"),
            "hello",
            opener=opener,
        )


def test_dispatch_is_a_noop_when_alerts_are_disabled(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_RUNNER_ALERTS", "0")
    monkeypatch.setattr(
        web_main, "send_telegram_message", lambda *_args: pytest.fail("must not send")
    )
    _insert_runner("run-1", "AAAA", score=80, entered_at="2026-09-10T14:00:00+00:00")

    result = web_main.dispatch_new_runner_alerts(scan_run_id="run-1")

    assert result == {
        "enabled": False,
        "baseline": False,
        "candidates": 0,
        "selected": 0,
        "status": "disabled",
    }


def test_dispatch_reports_missing_configuration(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    result = web_main.dispatch_new_runner_alerts(scan_run_id="run-1")

    assert result["status"] == "unconfigured"


def test_dispatch_takes_a_baseline_and_sends_only_the_triggering_run(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_runner("run-old", "OLD1", score=95, entered_at="2026-09-01T14:00:00+00:00")
    _insert_runner("run-old", "OLD2", score=90, entered_at="2026-09-01T14:01:00+00:00")
    _insert_runner("run-new", "AAAA", score=80, entered_at="2026-09-10T14:00:00+00:00")

    first = web_main.dispatch_new_runner_alerts(scan_run_id="run-new")

    assert first["baseline"] is True
    assert first["status"] == "sent"
    assert first["selected"] == 1
    assert len(sent) == 1
    assert "AAAA" in sent[0]
    assert "OLD1" not in sent[0]
    assert "OLD2" not in sent[0]

    second = web_main.dispatch_new_runner_alerts(scan_run_id="run-new")

    assert second["baseline"] is False
    assert second["status"] == "empty"
    assert len(sent) == 1


def test_dispatch_never_backfills_the_existing_board(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_runner("run-old", "OLD1", score=95, entered_at="2026-09-01T14:00:00+00:00")

    result = web_main.dispatch_new_runner_alerts(scan_run_id="run-nothing-new")

    assert result["baseline"] is True
    assert result["status"] == "empty"
    assert sent == []


def test_dispatch_applies_the_score_floor(alert_environment, monkeypatch: MonkeyPatch) -> None:
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_runner("run-1", "QUIET", score=42, entered_at="2026-09-10T14:00:00+00:00")

    result = web_main.dispatch_new_runner_alerts(scan_run_id="run-1")

    assert result["status"] == "empty"
    assert sent == []


def test_failed_delivery_retries_on_the_next_dispatch(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    attempts: list[str] = []

    def failing(config, text):
        attempts.append(text)
        raise RuntimeError("telegram is unreachable")

    monkeypatch.setattr(web_main, "send_telegram_message", failing)
    _insert_runner("run-1", "AAAA", score=80, entered_at="2026-09-10T14:00:00+00:00")

    first = web_main.dispatch_new_runner_alerts(scan_run_id="run-1")
    second = web_main.dispatch_new_runner_alerts(scan_run_id="run-1")

    assert first["status"] == "failed"
    assert second["status"] == "failed"
    assert len(attempts) == 2


def test_delivery_stops_after_the_maximum_attempts(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    attempts: list[str] = []

    def failing(config, text):
        attempts.append(text)
        raise RuntimeError("telegram is unreachable")

    monkeypatch.setattr(web_main, "send_telegram_message", failing)
    _insert_runner("run-1", "AAAA", score=80, entered_at="2026-09-10T14:00:00+00:00")

    statuses = [
        web_main.dispatch_new_runner_alerts(scan_run_id="run-1")["status"] for _ in range(4)
    ]

    assert statuses == ["failed", "failed", "failed", "empty"]
    assert len(attempts) == web_main.TELEGRAM_ALERT_MAX_ATTEMPTS


def test_delivery_failure_does_not_roll_back_the_recorded_entries(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    def failing(config, text):
        raise RuntimeError("telegram is unreachable")

    monkeypatch.setattr(web_main, "send_telegram_message", failing)
    _insert_runner("run-1", "AAAA", score=80, entered_at="2026-09-10T14:00:00+00:00")

    web_main.dispatch_new_runner_alerts(scan_run_id="run-1")

    with connection() as database:
        entries = database.execute("SELECT COUNT(*) AS count FROM pulse_entries").fetchone()
        deliveries = database.execute(
            "SELECT status,attempts FROM telegram_alert_deliveries WHERE ticker='AAAA'"
        ).fetchone()

    assert int(entries["count"]) == 1
    assert deliveries["status"] == "failed"
    assert int(deliveries["attempts"]) == 1
