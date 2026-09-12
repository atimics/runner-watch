from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime

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
    monkeypatch.setenv("TELEGRAM_API_TOKEN", "test-token")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setenv("TELEGRAM_MIN_SCORE", "60")
    monkeypatch.setenv("TELEGRAM_MAX_PER_RUN", "10")
    monkeypatch.setattr(web_main, "TELEGRAM_ANNOUNCE_BATCH_MIN", 1)
    monkeypatch.setattr(web_main, "TELEGRAM_ANNOUNCE_DEBOUNCE_MINUTES", 0)
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
    monkeypatch.setenv("TELEGRAM_API_TOKEN", " api-token ")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " chat ")
    monkeypatch.setenv("TELEGRAM_MIN_SCORE", "72.5")
    monkeypatch.setenv("TELEGRAM_MAX_PER_RUN", "4")

    config = telegram.config_from_env()

    assert config.bot_token == "api-token"
    assert config.chat_id == "chat"
    assert config.min_score == 72.5
    assert config.max_per_run == 4
    assert config.configured is True


def test_api_token_takes_precedence_over_the_legacy_name(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_TOKEN", "api-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "legacy-token")

    assert telegram.bot_token_from_env() == "api-token"


def test_legacy_bot_token_is_still_accepted(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_API_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "legacy-token")

    assert telegram.bot_token_from_env() == "legacy-token"


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


def test_format_market_report_post_includes_leaders_and_link() -> None:
    text = telegram.format_market_report_post(
        {
            "report_type": "pre_market",
            "label": "Pre-market briefing",
            "headline": "MSGM leads the pre-market board",
            "summary": "12 names cleared the scanner. 8 were green.",
            "report_day": "2026-09-11",
            "path": "/reports/2026-09-11/pre",
            "leaders": [
                {"ticker": "msgm", "change_pct": 12.4, "score": 72},
                {"ticker": "VTAK", "change_pct": 8.1, "score": 65},
            ],
        },
        origin="https://runners.rati.chat",
    )

    assert text.startswith("📋 Pre-market briefing")
    assert "MSGM leads the pre-market board" in text
    assert "$MSGM · +12.40% · score 72" in text
    assert "$VTAK · +8.10% · score 65" in text
    assert "https://runners.rati.chat/reports/2026-09-11/pre" in text


def test_format_public_report_post_includes_ticker_and_report_links() -> None:
    text = telegram.format_public_report_post(
        {
            "ticker": "cast",
            "headline": "A quiet tape with a loud filing",
            "public_id": "rep-cast",
        },
        origin="https://runners.rati.chat/",
    )

    assert "📄 New public report · $CAST" in text
    assert "A quiet tape with a loud filing" in text
    assert "https://runners.rati.chat/t/CAST" in text
    assert "https://runners.rati.chat/research/rep-cast" in text


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
    monkeypatch.delenv("TELEGRAM_API_TOKEN", raising=False)
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


def test_a_sweep_with_no_scan_still_delivers_a_stranded_runner(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    """A runner must not be stranded because its scan never finished.

    The dispatch used to be reachable only at the end of a completed scan. A
    scan takes the better part of an hour and only writes its row once it
    finishes, so a worker restart part way through lost the run and the alert
    with it. Called with no scan id at all, the dispatch still has to deliver.
    """

    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_runner("run-old", "OLD1", score=95, entered_at="2026-09-01T14:00:00+00:00")
    web_main.dispatch_new_runner_alerts(scan_run_id="run-nothing-new")
    assert sent == []

    _insert_runner("run-lost", "LOUD", score=80, entered_at="2026-09-10T14:00:00+00:00")

    swept = web_main.dispatch_new_runner_alerts()

    assert swept["status"] == "sent"
    assert swept["selected"] == 1
    assert len(sent) == 1
    assert "LOUD" in sent[0]

    assert web_main.dispatch_new_runner_alerts()["status"] == "empty"
    assert len(sent) == 1


def test_two_dispatches_at_once_post_once(alert_environment, monkeypatch: MonkeyPatch) -> None:
    """The scan tail and the sweep can both arrive at the dispatch.

    The gap between choosing runners and recording them as sent is wide enough
    for a second dispatch to choose the same ones, which would post the digest
    to the channel twice.
    """

    import threading

    sent: list[str] = []
    second: dict[str, object] = {}
    started = threading.Event()
    release = threading.Event()

    def _send(config: object, text: str) -> None:
        first_call = not sent
        sent.append(text)
        if first_call:
            # Hold the first dispatch inside the send so the second one arrives
            # while the runners it would pick are chosen but not yet recorded.
            started.set()
            release.wait(timeout=5)

    monkeypatch.setattr(web_main, "send_telegram_message", _send)
    _insert_runner("run-1", "LOUD", score=80, entered_at="2026-09-10T14:00:00+00:00")

    worker = threading.Thread(target=lambda: second.update(web_main.dispatch_new_runner_alerts()))
    first_result: dict[str, object] = {}
    holder = threading.Thread(
        target=lambda: first_result.update(web_main.dispatch_new_runner_alerts(scan_run_id="run-1"))
    )
    holder.start()
    assert started.wait(timeout=5), "the first dispatch never reached the send"
    worker.start()
    worker.join(timeout=5)
    release.set()
    holder.join(timeout=5)

    assert second, (
        "the second dispatch never returned: it was not turned away and is "
        "sitting behind the first one instead"
    )
    assert second["status"] == "busy"
    assert first_result["status"] == "sent"
    assert len(sent) == 1


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


def _insert_market_report(
    report_id: str,
    *,
    report_day: str = "2026-09-11",
    report_type: str = "pre_market",
    headline: str = "MSGM leads the pre-market board",
    created_at: str = "2026-09-11T08:15:00+00:00",
) -> None:
    with connection() as database:
        database.execute(
            """
            INSERT INTO market_session_reports(
                id,report_day,report_type,source_scan_run_id,comparison_scan_run_id,
                as_of,headline,summary,metrics_json,leaders_json,turns_json,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report_id,
                report_day,
                report_type,
                "scan-1",
                None,
                created_at,
                headline,
                "12 names cleared the scanner. 8 were green.",
                '{"candidates":12,"green":8}',
                json.dumps([{"ticker": "MSGM", "change_pct": 12.4, "score": 72, "rank": 1}]),
                "[]",
                created_at,
                created_at,
            ),
        )


def _insert_public_report(
    report_id: str,
    ticker: str,
    *,
    visibility: str = "public",
    customer_inference: int = 0,
    created_at: str = "2026-09-11T16:00:00+00:00",
) -> None:
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            ("flash-user", "flash_user", "Flash user", "active", created_at),
        )
        database.execute(
            """
            INSERT INTO research_commissions(
                id,public_id,user_id,ticker,evidence_key,status,requested_model,model,
                headline,summary,visibility,customer_inference,created_at,updated_at,
                completed_at,published_at
            ) VALUES(?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report_id,
                f"pub-{report_id}",
                "flash-user",
                ticker,
                f"evidence-{report_id}",
                "test-model",
                "test-model",
                f"{ticker} report",
                "A public note.",
                visibility,
                customer_inference,
                created_at,
                created_at,
                created_at,
                created_at if visibility == "public" else None,
            ),
        )


def test_dispatch_posts_a_new_market_report_and_skips_the_old_one(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_market_report(
        "old-pre", created_at="2026-09-10T08:15:00+00:00", report_day="2026-09-10"
    )

    first = web_main.dispatch_telegram_posts()

    assert first["market_reports"]["baseline"] is True
    assert first["market_reports"]["status"] == "empty"
    assert sent == []

    _insert_market_report("new-pre", headline="CAST leads the pre-market board")
    second = web_main.dispatch_telegram_posts()

    assert second["status"] == "sent"
    assert second["market_reports"]["status"] == "sent"
    assert len(sent) == 1
    assert "Pre-market briefing" in sent[0]
    assert "CAST leads the pre-market board" in sent[0]
    assert f"{web_main.RUNNERS_ORIGIN}/reports/2026-09-11/pre" in sent[0]
    assert web_main.dispatch_telegram_posts()["market_reports"]["status"] == "empty"
    assert len(sent) == 1


def test_dispatch_posts_a_new_public_report_and_ignores_private_ones(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_public_report("old", "OLD1")
    first = web_main.dispatch_telegram_posts()
    assert first["research_reports"]["baseline"] is True
    assert sent == []

    _insert_public_report("private", "HIDE", visibility="private")
    _insert_public_report("own-model", "OWN", customer_inference=1)
    _insert_public_report("fresh", "CAST")
    result = web_main.dispatch_telegram_posts()

    assert result["research_reports"]["status"] == "sent"
    assert len(sent) == 1
    assert "$CAST" in sent[0]
    assert f"{web_main.RUNNERS_ORIGIN}/research/pub-fresh" in sent[0]
    assert "HIDE" not in sent[0]
    assert "OWN" not in sent[0]


def test_dispatch_queues_a_free_report_for_each_new_runner(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_RUNNER_REPORTS_PER_DAY", "20")
    monkeypatch.setattr(web_main, "TELEGRAM_RUNNER_REPORTS_PER_DAY", 20)
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: None)
    monkeypatch.setattr(web_main, "_flash_provider_ready", lambda *args, **kwargs: True)
    created: list[str] = []

    def fake_create(user_id, ticker, **kwargs):
        created.append(ticker)
        assert user_id == web_main.MACHINE_USER_ID
        assert kwargs["charge"] is False
        assert kwargs["trigger"] == "telegram_runner"
        return {"id": f"rep-{ticker}"}, True

    monkeypatch.setattr(web_main, "_create_research_commission", fake_create)
    enqueued: list[str] = []
    monkeypatch.setattr(web_main, "_enqueue_research_job_sync", enqueued.append)
    monkeypatch.setattr(web_main, "ensure_machine_trader", lambda: None)
    _insert_runner("run-1", "CAST", score=80, entered_at="2026-09-10T14:00:00+00:00")
    _insert_runner("run-1", "QUIET", score=20, entered_at="2026-09-10T14:01:00+00:00")

    result = web_main.dispatch_new_runner_alerts(scan_run_id="run-1")

    assert result["status"] == "sent"
    assert created == ["CAST"]
    assert enqueued == ["rep-CAST"]
    assert result["reports"] == {"queued": 1, "skipped": 0, "tickers": ["CAST"]}


def test_runner_report_queue_respects_the_daily_cap(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_RUNNER_REPORTS_PER_DAY", "1")
    monkeypatch.setattr(web_main, "TELEGRAM_RUNNER_REPORTS_PER_DAY", 1)
    monkeypatch.setattr(web_main, "_flash_provider_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(web_main, "ensure_machine_trader", lambda: None)
    day = "2026-09-11"
    monkeypatch.setattr(web_main, "now", lambda: datetime(2026, 9, 11, tzinfo=UTC))
    _insert_public_report("used", "OLD1", created_at=f"{day}T12:00:00+00:00")
    with connection() as database:
        database.execute(
            "UPDATE research_commissions SET trigger='telegram_runner', "
            "report_day=? WHERE id='used'",
            (day,),
        )
    created: list[str] = []

    def fake_create(user_id, ticker, **kwargs):
        created.append(ticker)
        return {"id": f"rep-{ticker}"}, True

    monkeypatch.setattr(web_main, "_create_research_commission", fake_create)
    monkeypatch.setattr(web_main, "_enqueue_research_job_sync", lambda report_id: None)

    result = web_main._queue_telegram_runner_reports(["AAAA", "BBBB"])

    assert result["queued"] == 0
    assert result["skipped"] == 2
    assert created == []


def _announce_reply(text: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "announce",
                                "arguments": json.dumps({"text": text}),
                            },
                        }
                    ],
                }
            }
        ]
    }


def test_a_lone_runner_waits_for_a_batch_then_goes_out_with_it(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(web_main, "TELEGRAM_ANNOUNCE_BATCH_MIN", 2)
    monkeypatch.setattr(web_main, "TELEGRAM_ANNOUNCE_DEBOUNCE_MINUTES", 30)
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    recent = datetime.now(UTC).isoformat()
    _insert_runner("run-1", "AAAA", score=80, entered_at=recent)

    waiting = web_main.dispatch_telegram_posts(scan_run_id="run-1")

    assert waiting["status"] == "waiting"
    assert waiting["announcement"]["status"] == "waiting"
    assert sent == []

    _insert_runner("run-1", "BBBB", score=81, entered_at=recent)
    sent_batch = web_main.dispatch_telegram_posts(scan_run_id="run-1")

    assert sent_batch["status"] == "sent"
    assert sent_batch["announcement"]["count"] == 2
    assert len(sent) == 1
    assert "AAAA" in sent[0]
    assert "BBBB" in sent[0]


def test_a_report_batch_goes_out_as_one_message_not_one_per_report(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(web_main, "TELEGRAM_ANNOUNCE_BATCH_MIN", 2)
    monkeypatch.setattr(web_main, "TELEGRAM_ANNOUNCE_DEBOUNCE_MINUTES", 30)
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    web_main.dispatch_telegram_posts()  # baseline the empty board
    recent = datetime.now(UTC).isoformat()
    _insert_market_report("pre-1", created_at=recent)
    _insert_public_report("one", "CAST", created_at=recent)

    result = web_main.dispatch_telegram_posts()

    assert result["status"] == "sent"
    assert result["announcement"]["count"] == 2
    assert len(sent) == 1
    assert "Pre-market briefing" in sent[0]
    assert "$CAST" in sent[0]
    assert f"{web_main.RUNNERS_ORIGIN}/reports/2026-09-11/pre" in sent[0]
    assert f"{web_main.RUNNERS_ORIGIN}/research/pub-one" in sent[0]


def test_the_announcement_uses_dash_when_the_model_answers(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        web_main,
        "_telegram_chat_completion",
        lambda body: _announce_reply("two names just landed"),
    )
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_runner("run-1", "AAAA", score=80, entered_at=datetime.now(UTC).isoformat())

    web_main.dispatch_telegram_posts(scan_run_id="run-1")

    assert sent == ["two names just landed"]


def test_the_announcement_falls_back_when_the_model_fails(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "test-key")

    def boom(body):
        raise RuntimeError("provider down")

    monkeypatch.setattr(web_main, "_telegram_chat_completion", boom)
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_runner("run-1", "AAAA", score=80, entered_at=datetime.now(UTC).isoformat())

    web_main.dispatch_telegram_posts(scan_run_id="run-1")

    assert len(sent) == 1
    assert "$AAAA" in sent[0]


def test_a_new_build_announces_itself_once(alert_environment, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_RELEASE_ANNOUNCEMENTS", "1")
    monkeypatch.setattr(web_main, "APP_BUILD_SHA", "sha-one")
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))

    assert web_main.dispatch_release_announcement()["status"] == "baseline"
    assert sent == []

    monkeypatch.setattr(web_main, "APP_BUILD_SHA", "sha-two")
    second = web_main.dispatch_release_announcement()

    assert second["status"] == "sent"
    assert len(sent) == 1
    assert "sha-two" in sent[0]
    assert web_main.dispatch_release_announcement()["status"] == "already"
    assert len(sent) == 1


def test_release_announcements_stay_off_until_enabled(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_RELEASE_ANNOUNCEMENTS", raising=False)

    assert web_main.dispatch_release_announcement()["status"] == "off"


def test_batch_readiness_waits_for_a_batch_or_the_debounce() -> None:
    assert telegram.announcement_batch_ready(0, 999) is False
    assert telegram.announcement_batch_ready(2, 0, min_items=2) is True
    assert telegram.announcement_batch_ready(1, 5, min_items=2, debounce_minutes=30) is False
    assert telegram.announcement_batch_ready(1, 31, min_items=2, debounce_minutes=30) is True


def test_a_plain_announcement_names_what_landed_and_links_it() -> None:
    text = telegram.format_update_announcement(
        {
            "runners": [
                {
                    "ticker": "MSGM",
                    "change_pct": 34.0,
                    "relative_volume": 8.4,
                    "path": "/t/MSGM",
                }
            ],
            "reports": [
                {
                    "label": "Pre-market briefing",
                    "headline": "CAST leads",
                    "path": "/reports/2026-09-11/pre",
                }
            ],
        },
        origin="https://runners.example",
    )

    assert "2 new on the board" in text
    assert "$MSGM" in text
    assert "+34.00%" in text
    assert "RVOL 8.4x" in text
    assert "https://runners.example/t/MSGM" in text
    assert "Pre-market briefing: CAST leads" in text
    assert "https://runners.example/reports/2026-09-11/pre" in text


def test_a_failed_release_announcement_retries_then_stays_sent(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_RELEASE_ANNOUNCEMENTS", "1")
    monkeypatch.setattr(web_main, "APP_BUILD_SHA", "sha-one")
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: None)
    web_main.dispatch_release_announcement()

    monkeypatch.setattr(web_main, "APP_BUILD_SHA", "sha-two")
    attempts: list[str] = []

    def failing(config, text):
        attempts.append(text)
        raise RuntimeError("telegram is unreachable")

    monkeypatch.setattr(web_main, "send_telegram_message", failing)
    assert web_main.dispatch_release_announcement()["status"] == "failed"
    assert web_main.dispatch_release_announcement()["status"] == "failed"

    monkeypatch.setattr(
        web_main, "send_telegram_message", lambda config, text: attempts.append(text)
    )
    assert web_main.dispatch_release_announcement()["status"] == "sent"
    assert len(attempts) == 3
    assert web_main.dispatch_release_announcement()["status"] == "already"


def test_a_model_announcement_that_names_a_stray_ticker_is_rejected(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        web_main,
        "_telegram_chat_completion",
        lambda body: _announce_reply("$NVDA is flying"),
    )
    sent: list[str] = []
    monkeypatch.setattr(web_main, "send_telegram_message", lambda config, text: sent.append(text))
    _insert_runner("run-1", "AAAA", score=80, entered_at=datetime.now(UTC).isoformat())

    web_main.dispatch_telegram_posts(scan_run_id="run-1")

    assert len(sent) == 1
    assert "$NVDA" not in sent[0]
    assert "$AAAA" in sent[0]


def _runner_report_queue(monkeypatch: MonkeyPatch) -> list[tuple[str, int | None]]:
    calls: list[tuple[str, int | None]] = []

    def fake_create(user_id, ticker, **kwargs):
        calls.append((ticker, kwargs.get("exclusive_minutes")))
        return {"id": f"rep-{ticker}"}, True

    monkeypatch.setattr(web_main, "TELEGRAM_RUNNER_REPORTS_PER_DAY", 20)
    monkeypatch.setattr(web_main, "_flash_provider_ready", lambda *args, **kwargs: True)
    monkeypatch.setattr(web_main, "ensure_machine_trader", lambda: None)
    monkeypatch.setattr(web_main, "_enqueue_research_job_sync", lambda report_id: None)
    monkeypatch.setattr(web_main, "_create_research_commission", fake_create)
    return calls


def test_only_the_best_few_runners_get_a_free_report(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    calls = _runner_report_queue(monkeypatch)
    monkeypatch.setattr(web_main, "TELEGRAM_RUNNER_REPORTS_PER_RUN", 3)

    result = web_main._queue_telegram_runner_reports(["A1", "B2", "C3", "D4", "E5"])

    assert [ticker for ticker, _ in calls] == ["A1", "B2", "C3"]
    assert result["queued"] == 3


def test_free_reports_are_staggered_so_they_do_not_land_together(
    alert_environment, monkeypatch: MonkeyPatch
) -> None:
    calls = _runner_report_queue(monkeypatch)
    monkeypatch.setattr(web_main, "TELEGRAM_RUNNER_REPORTS_PER_RUN", 3)
    monkeypatch.setattr(web_main, "TELEGRAM_RUNNER_REPORT_STAGGER_MINUTES", 10)

    web_main._queue_telegram_runner_reports(["A1", "B2", "C3"])

    assert calls == [("A1", 0), ("B2", 10), ("C3", 20)]


def test_a_free_report_can_skip_the_paid_private_window() -> None:
    start = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

    assert web_main._exclusive_until_for(start) == "2026-09-11T13:00:00+00:00"
    assert web_main._exclusive_until_for(start, 0) == "2026-09-11T12:00:00+00:00"
    assert web_main._exclusive_until_for(start, 10) == "2026-09-11T12:10:00+00:00"
