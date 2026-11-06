from __future__ import annotations

import json
from datetime import timedelta
from io import BytesIO
from urllib.error import HTTPError

import pytest

from runner_watch.ingestion import SourceFetch
from runner_watch.source_catalog import DEFAULT_SOURCE_POLICIES
from runner_web import dash_jobs, dash_research, dash_sources, db, main
from runner_web.db import connection, init_db
from runner_web.ingestion import record_source_fetch, register_source
from runner_web.telegram import TelegramConfig, TelegramDeliveryError, send_reply
from tests.test_dash_research import NOW, SCOPE, run, thesis, turn


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "followthrough.db")
    monkeypatch.setenv("TELEGRAM_CHANNEL_INTERVAL_SECONDS", "0")
    init_db()


def job(identity="desk-test"):
    with connection() as database:
        return dict(
            database.execute("SELECT * FROM dash_research_jobs WHERE id=?", (identity,)).fetchone()
        )


def desk(monkeypatch, sender):
    monkeypatch.setattr(main, "DASH_DESK_NOTES_ENABLED", True)
    monkeypatch.setattr(main, "DASH_DECISIONS_ENABLED", False)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "fixture")
    monkeypatch.setenv("TELEGRAM_API_TOKEN", "fixture")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "fixture")
    monkeypatch.setattr(main, "telegram_room_chat_id", lambda: -100)
    monkeypatch.setattr(main, "dash_world", lambda *a: {"changes": {"any": False}})
    monkeypatch.setattr(main, "send_telegram_reply", sender)


def test_restart_resumes_evidence_then_commits_note_and_finding_together():
    reads = []
    with pytest.raises(StopIteration):
        run(
            [
                turn(("expand", {"node": "ticker:TEST"})),
                turn(("remember_thesis", thesis(status="closed"))),
            ],
            job_key="desk-test",
            job_chat_id=-100,
            expand=lambda node: reads.append(node) or {"price": 2},
            proactive=True,
        )
    assert dash_research.notebook(SCOPE, NOW) == []
    assert json.loads(job()["state_json"])["notes"]["ticker:TEST"]["status"] == "closed"
    result, _ = run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("reply", {"text": "The new volume changed my view."})),
        ],
        job_key="desk-test",
        job_chat_id=-100,
        proactive=True,
        expand=lambda node: pytest.fail("The saved recent check is available"),
        at_override=NOW + timedelta(minutes=1),
    )
    assert result["action"] == "reply"
    assert reads == ["ticker:TEST"]
    assert dash_research.notebook(SCOPE, NOW)[0]["status"] == "closed"
    with connection() as database:
        assert database.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0] == 1
    again, requests = run([], job_key="desk-test", job_chat_id=-100, proactive=True)
    assert again == result and requests == []


def test_atomic_queue_failure_preserves_the_original_due_note(monkeypatch):
    dash_research.save_thesis(
        SCOPE, thesis(), {"ticker:TEST": {"price": 1}}, NOW - timedelta(hours=2)
    )
    monkeypatch.setattr(
        dash_jobs, "enqueue_cards", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("queue"))
    )
    with pytest.raises(RuntimeError, match="queue"):
        run(
            [
                turn(("expand", {"node": "ticker:TEST"})),
                turn(
                    ("remember_thesis", thesis(status="closed")),
                    ("reply", {"text": "The view changed."}),
                ),
            ],
            job_key="desk-test",
            job_chat_id=-100,
            proactive=True,
        )
    assert dash_research.notebook(SCOPE, NOW)[0]["status"] == "watching"
    assert job()["status"] == "pending"


def test_delivery_retries_the_frozen_finding_after_review_closes(monkeypatch):
    run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(
                ("remember_thesis", thesis(status="closed")),
                ("reply", {"text": "My view changed."}),
            ),
        ],
        job_key="desk-test",
        job_chat_id=-100,
        proactive=True,
    )
    calls = []

    def sender(*args, **kwargs):
        calls.append(args[2])
        assert kwargs["confirmed"] is True
        if len(calls) == 1:
            raise TelegramDeliveryError("retry", retry_after=60)
        return 42

    desk(monkeypatch, sender)
    monkeypatch.setattr(
        main, "_generate_desk_note", lambda world: pytest.fail("Finding already saved")
    )
    assert main.post_dash_desk_note(at=NOW)["status"] == "retry"
    assert main.post_dash_desk_note(at=NOW + timedelta(minutes=2))["status"] == "sent"
    assert calls == ["My view changed.", "My view changed."]
    assert main.post_dash_desk_note(at=NOW + timedelta(hours=2))["status"] == "quiet"
    with connection() as database:
        assert database.execute("SELECT message_id FROM telegram_outbox").fetchone()[0] == 42


def test_uncertain_delivery_retains_a_finding_for_receipt_review(monkeypatch):
    run(
        [turn(("expand", {"node": "board"})), turn(("reply", {"text": "A useful finding."}))],
        job_key="desk-test",
        job_chat_id=-100,
        proactive=True,
    )
    desk(monkeypatch, lambda *a, **k: None)
    assert main.post_dash_desk_note(at=NOW)["status"] == "uncertain"
    with connection() as database:
        row = database.execute("SELECT status,text FROM telegram_outbox").fetchone()
        assert row["status"] == "uncertain" and row["text"] == "A useful finding."


def test_unavailable_review_and_ready_finding_finish_independently():
    for node in ("ticker:GONE", "ticker:TEST", "ticker:LATER"):
        dash_research.save_thesis(
            SCOPE, thesis(node), {node: {"price": 1}}, NOW - timedelta(hours=2)
        )
    result, _ = run(
        [
            turn(("expand", {"node": "ticker:GONE"}), ("expand", {"node": "ticker:TEST"})),
            turn(
                (
                    "finish_review",
                    {
                        "node": "ticker:GONE",
                        "outcome": "deferred_source",
                        "reason": "Source is unavailable.",
                    },
                ),
                ("remember_thesis", thesis()),
                ("reply", {"text": "The fresh evidence supports my view."}),
            ),
        ],
        proactive=True,
        expand=lambda n: {"known": False} if n.endswith("GONE") else {"price": 2},
    )
    assert result["action"] == "reply"
    notes = {n["node"]: n for n in dash_research.notebook(SCOPE, NOW)}
    assert notes["ticker:GONE"]["review_outcome"] == "deferred_source"
    assert notes["ticker:TEST"]["review_outcome"] == "finding_ready"
    assert notes["ticker:LATER"]["review_outcome"] == "incomplete"
    assert notes["ticker:LATER"]["reviewed_at"] == (NOW - timedelta(hours=2)).isoformat()


def test_empty_model_output_preserves_unfinished_review():
    dash_research.save_thesis(
        SCOPE, thesis(), {"ticker:TEST": {"price": 1}}, NOW - timedelta(hours=2)
    )
    empty = {"choices": [{"message": {"content": ""}}]}
    result, _ = run([empty, empty], proactive=True, allow_text=True)
    assert result["why"] == "research_incomplete"
    assert dash_research.notebook(SCOPE, NOW)[0]["review_outcome"] == "incomplete"


def test_job_claim_and_spending_limit(monkeypatch):
    first = dash_jobs.claim("desk-test", SCOPE, "desk", NOW)
    assert dash_jobs.claim("desk-test", SCOPE, "desk", NOW) is None
    dash_jobs.release("desk-test", first["token"], NOW)
    monkeypatch.setattr(dash_jobs, "MAX_ATTEMPTS", 1)
    result, requests = run(
        [],
        job_key="desk-test",
        job_chat_id=-100,
        proactive=True,
        at_override=NOW + timedelta(minutes=2),
    )
    assert result["why"] == "research_incomplete" and requests == []
    assert job()["status"] == "complete"


def store_source(*, url=None, text=None, ticker="TEST"):
    url = url or "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/exhibit.htm"
    stamp = NOW.isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO sec_filings(accession,cik,ticker,company,form,kind,sentiment,score,title,"
            "filed_at,filing_url,created_at,updated_at) "
            "VALUES(?,1,?,'Test','8-K','filing','neutral',"
            "0,'Contract terms',?,?,?,?) ON CONFLICT DO NOTHING",
            (ticker, ticker, stamp, url.rsplit("/", 1)[0] + "/index.htm", stamp, stamp),
        )
    register_source(
        next(
            p for p in DEFAULT_SOURCE_POLICIES if p.source == "sec" and p.feed == "filing_document"
        )
    )
    record_source_fetch(
        SourceFetch.success(
            source="sec",
            feed="filing_document",
            locator=url,
            started_at=NOW,
            payload=(
                text
                or "<html><p>A pilot contract.</p>"
                "<p>Payment terms: $5 million after acceptance.</p>"
                "<p>Acceptance requires regulatory approval.</p></html>"
            ).encode(),
            content_type="text/html",
        )
    )
    return url


def test_source_passages_include_terms_dates_and_exact_version():
    url = store_source()
    listing = dash_sources.list_sources("TEST")
    assert len(listing["sources"]) == 1
    ref = listing["sources"][0]
    read = dash_sources.read_source(ref, "payment terms")
    assert read["source_url"] == url and read["published_at"] == NOW.isoformat()
    assert "Payment terms: $5 million after acceptance." in str(read["passages"])
    assert "regulatory approval" in str(read["passages"])
    assert read["content_hash"] == ref["content_hash"]
    rendered, missing = dash_sources.render_citations(
        "My view " + read["citation"], {"source:" + ref["evidence_id"]: read}
    )
    assert url in rendered and not missing


def test_source_access_tracks_approval_and_ticker_identity():
    store_source()
    assert dash_sources.list_sources("OTHER")["sources"] == []
    ref = dash_sources.list_sources("TEST")["sources"][0]
    with connection() as database:
        database.execute(
            "UPDATE source_registry SET display_policy='internal_review_only' WHERE source='sec'"
        )
    assert dash_sources.list_sources("TEST")["sources"] == []
    assert "error" in dash_sources.read_source(ref, "payment")


def test_source_read_requires_a_listed_id_and_citations_require_a_passage():
    result, requests = run(
        [
            turn(("read_source", {"evidence_id": "guess", "question": "payment"})),
            turn(("reply", {"text": "A claim [source:guess]"})),
            turn(("reply", {"text": "The source check remains open."})),
        ]
    )
    assert "returned by list_sources" in requests[1]["messages"][-1]["content"]
    assert "Read the cited sources" in requests[2]["messages"][-1]["content"]
    assert result["text"] == "The source check remains open."


def test_full_source_tool_path_saves_reference_with_the_view():
    store_source()
    ref = dash_sources.list_sources("TEST")["sources"][0]
    result, _ = run(
        [
            turn(("expand", {"node": "ticker:TEST"}), ("list_sources", {"ticker": "TEST"})),
            turn(("read_source", {"evidence_id": ref["evidence_id"], "question": "payment"})),
            turn(
                ("remember_thesis", thesis()),
                (
                    "reply",
                    {
                        "text": "The payment depends on approval. [source:"
                        + ref["evidence_id"]
                        + "]"
                    },
                ),
            ),
        ],
        at_override=NOW + timedelta(days=1),
    )
    assert ref["source_url"] in result["text"]
    assert (
        dash_research.notebook(SCOPE, NOW + timedelta(days=1))[0]["source_references"][0][
            "content_hash"
        ]
        == ref["content_hash"]
    )


def test_confirmed_reply_handles_rate_limits_and_parse_fallback():
    config = TelegramConfig("fixture", "-100")

    def limited(request, **kw):
        raise HTTPError(
            request.full_url, 429, "limited", {}, BytesIO(b'{"parameters":{"retry_after":120}}')
        )

    with pytest.raises(TelegramDeliveryError) as caught:
        send_reply(config, -100, "Finding", confirmed=True, opener=limited)
    assert caught.value.status == "retry" and caught.value.retry_after == 120
    payloads = []

    def receive(request, **kw):
        payloads.append(json.loads(request.data))
        if len(payloads) == 1:
            return BytesIO(b'{"ok":false,"error_code":400,"description":"parse error"}')
        return BytesIO(b'{"ok":true,"result":{"message_id":123}}')

    assert (
        send_reply(config, -100, "Finding", html="<b>Finding</b>", confirmed=True, opener=receive)
        == 123
    )
    assert payloads[0]["parse_mode"] == "HTML" and "parse_mode" not in payloads[1]
