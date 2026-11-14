import asyncio
import json
from datetime import timedelta

import pytest

from runner_web import dash_direction, dash_jobs, dash_research, main
from runner_web.db import connection
from runner_web.telegram_outbox import enqueue_cards
from tests.test_dash_direction import agenda
from tests.test_dash_followthrough import desk, job
from tests.test_dash_research import NOW, SCOPE, fresh_db, run, thesis, turn  # noqa: F401


def scheduled_thesis(**changes):
    args = thesis(**changes)
    args.pop("review_minutes", None)
    args["review_at"] = (NOW + timedelta(hours=6)).isoformat()
    return args


def test_tool_durable_result_and_outbox_share_the_same_review_instant():
    result, requests = run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("remember_thesis", scheduled_thesis())),
            turn(("reply", {"text": "The move needs more volume.\n[review:ticker:TEST]"})),
        ],
        job_key="desk-test",
        job_chat_id=-100,
        proactive=True,
        automatic_reviews=True,
    )
    saved = dash_research.notebook(SCOPE, NOW)[0]
    tool_result = json.loads(requests[2]["messages"][-1]["content"])
    assert tool_result["schedule"]["review_at"] == saved["review_at"]
    assert result["review_schedule"] == {"ticker:TEST": saved["review_at"]}
    assert saved["announced_review_at"] == saved["review_at"]
    assert "Nov 6, 2026 at 17:00 EST" in result["text"]
    assert "[review:" not in result["text"]
    with connection() as database:
        text = database.execute("SELECT text FROM telegram_outbox").fetchone()[0]
    assert text == json.loads(job()["result_json"])["text"] == result["text"]


def test_handwritten_promise_cannot_replace_a_required_schedule_marker():
    result, requests = run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(
                ("remember_thesis", thesis(review_minutes=720)),
                ("reply", {"text": "I'll check at 23:30 ET."}),
            ),
            turn(("reply", {"text": "[review:ticker:TEST]"})),
        ],
        automatic_reviews=True,
    )
    assert "[review:...] marker" in requests[2]["messages"][-1]["content"]
    assert "23:30" not in result["text"]
    assert result["review_schedule"]["ticker:TEST"] == (NOW + timedelta(hours=12)).isoformat()


def test_other_rooms_and_topics_only_offer_the_next_conversation():
    result, _ = run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("remember_thesis", thesis()), ("reply", {"text": "[review:ticker:TEST]"})),
        ],
        automatic_reviews=False,
    )
    assert result["text"] == "I'll revisit this in our next conversation."
    assert "announced_review_at" not in dash_research.notebook(SCOPE, NOW)[0]


def test_a_stale_checkpoint_announces_the_newer_winning_thesis():
    with pytest.raises(StopIteration):
        run(
            [turn(("expand", {"node": "ticker:TEST"})), turn(("remember_thesis", thesis()))],
            job_key="desk-test",
        )
    later = NOW + timedelta(minutes=2)
    dash_research.save_thesis(
        SCOPE,
        thesis(thesis="The newer view wins.", review_minutes=120),
        {"ticker:TEST": {"price": 3}},
        later,
    )
    result, _ = run(
        [turn(("reply", {"text": "[review:ticker:TEST]"}))],
        job_key="desk-test",
        at_override=later + timedelta(minutes=1),
        automatic_reviews=True,
    )
    saved = dash_research.notebook(SCOPE, later)[0]
    assert saved["thesis"] == "The newer view wins."
    assert result["review_schedule"]["ticker:TEST"] == saved["review_at"]
    assert "13:02 EST" in result["text"]


def test_a_stale_checkpoint_announces_the_newer_winning_agenda():
    with pytest.raises(StopIteration):
        run([turn(("set_direction", agenda()))], job_key="desk-test")
    later = NOW + timedelta(minutes=2)
    new = dash_direction.propose(agenda(review_minutes=120), later)["direction"]
    with connection() as database:
        dash_direction.finish(database, SCOPE, new, later)
    result, _ = run(
        [turn(("reply", {"text": "[review:direction]"}))],
        job_key="desk-test",
        at_override=later + timedelta(minutes=1),
        automatic_reviews=True,
    )
    assert result["review_schedule"]["direction"] == new["review_at"]
    assert "13:02 EST" in result["text"]


def test_announcement_and_schedule_roll_back_if_the_outbox_fails(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("queue")

    monkeypatch.setattr(dash_jobs, "enqueue_cards", fail)
    with pytest.raises(RuntimeError, match="queue"):
        run(
            [
                turn(("expand", {"node": "ticker:TEST"})),
                turn(("remember_thesis", thesis()), ("reply", {"text": "[review:ticker:TEST]"})),
            ],
            job_key="desk-test",
            job_chat_id=-100,
            proactive=True,
            automatic_reviews=True,
        )
    assert dash_research.notebook(SCOPE, NOW) == []
    assert job()["status"] == "pending"


def test_early_quiet_pass_crossing_the_deadline_preserves_the_promised_review():
    run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("remember_thesis", thesis()), ("set_direction", agenda())),
            turn(("reply", {"text": "[review:ticker:TEST]\n[review:direction]"})),
        ],
        job_key="announced",
        automatic_reviews=True,
    )
    started, completed = NOW + timedelta(minutes=50), NOW + timedelta(minutes=61)
    run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(
                ("remember_thesis", thesis(review_minutes=120)),
                ("set_direction", agenda(review_minutes=120)),
                ("hold", {"why": "Unchanged."}),
            ),
        ],
        job_key="early",
        proactive=True,
        at_override=started,
        clock=lambda: completed,
    )
    note = dash_research.notebook(SCOPE, completed)[0]
    assert note["review_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert dash_research.due_notes([note], completed) == [note]
    assert dash_direction.read(SCOPE)["review_at"] == note["review_at"]
    assert dash_direction.due(SCOPE, completed) is True
    assert (
        dash_jobs.completed_result("early", SCOPE)["research"]["completed_at"]
        == completed.isoformat()
    )


def test_incomplete_legacy_review_defers_without_losing_its_revision(monkeypatch):
    dash_research.save_thesis(
        SCOPE, thesis(), {"ticker:TEST": {"price": 1}}, NOW - timedelta(hours=2)
    )
    with connection() as database:
        database.execute(
            "UPDATE worker_state SET updated_at=? WHERE key LIKE 'dash_thesis:%'",
            ((NOW - timedelta(hours=2) + timedelta(seconds=30)).isoformat(),),
        )
    monkeypatch.setattr(dash_jobs, "MAX_ATTEMPTS", 1)
    result, _ = run([{}, {}], job_key="desk-test", proactive=True)
    assert result["research"]["outcome"] == "incomplete"
    note = dash_research.notebook(SCOPE, NOW)[0]
    assert note["review_at"] == (NOW + timedelta(minutes=30)).isoformat()
    assert dash_research.due_notes([note], NOW) == []


def test_early_quiet_retirement_keeps_the_announced_check():
    run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("remember_thesis", thesis()), ("reply", {"text": "[review:ticker:TEST]"})),
        ],
        automatic_reviews=True,
    )
    early = NOW + timedelta(minutes=30)
    run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(
                (
                    "finish_review",
                    {"node": "ticker:TEST", "outcome": "retired", "reason": "Nothing further yet."},
                ),
                ("hold", {"why": "Quiet early review."}),
            ),
        ],
        at_override=early,
    )
    note = dash_research.notebook(SCOPE, early)[0]
    assert note["status"] == "watching" and note["pending_status"] == "closed"
    assert note["review_at"] == (NOW + timedelta(hours=1)).isoformat()
    assert dash_research.due_notes([note], NOW + timedelta(hours=1)) == [note]


def test_failed_announced_review_keeps_a_bounded_retry_time(monkeypatch):
    run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("remember_thesis", thesis()), ("set_direction", agenda())),
            turn(("reply", {"text": "[review:ticker:TEST]\n[review:direction]"})),
        ],
        automatic_reviews=True,
    )
    due = NOW + timedelta(hours=1)
    monkeypatch.setattr(dash_jobs, "MAX_ATTEMPTS", 1)
    result, _ = run([{}, {}], job_key="desk-test", proactive=True, at_override=due)
    assert result["research"]["outcome"] == "incomplete"
    note = dash_research.notebook(SCOPE, due)[0]
    assert note["announced_review_at"] == due.isoformat()
    assert note["review_at"] == (due + timedelta(minutes=30)).isoformat()
    assert dash_research.due_notes([note], due + timedelta(minutes=1)) == []
    assert not dash_direction.due(SCOPE, due + timedelta(minutes=1))
    assert dash_direction.due(SCOPE, due + timedelta(minutes=30))


def test_a_full_promised_notebook_cannot_silently_evict_a_check(monkeypatch):
    monkeypatch.setattr(dash_research, "MAX_NOTES", 2)
    run(
        [
            turn(("expand", {"node": "ticker:ONE"}), ("expand", {"node": "ticker:TWO"})),
            turn(
                ("remember_thesis", thesis("ticker:ONE")), ("remember_thesis", thesis("ticker:TWO"))
            ),
            turn(("reply", {"text": "[review:ticker:ONE]\n[review:ticker:TWO]"})),
        ],
        automatic_reviews=True,
    )
    result = dash_research.save_thesis(
        SCOPE, thesis("ticker:THREE"), {"ticker:THREE": {"price": 1}}, NOW
    )
    assert "outstanding review" in result["error"]
    assert {note["node"] for note in dash_research.notebook(SCOPE, NOW)} == {
        "ticker:ONE",
        "ticker:TWO",
    }
    result, _ = run(
        [
            turn(("expand", {"node": "ticker:THREE"})),
            turn(
                ("remember_thesis", thesis("ticker:THREE")),
                ("hold", {"why": "Finish existing checks first."}),
            ),
        ],
        job_key="full-notebook",
    )
    assert result["action"] == "hold"
    assert dash_jobs.completed_result("full-notebook", SCOPE) == result
    assert {note["node"] for note in dash_research.notebook(SCOPE, NOW)} == {
        "ticker:ONE",
        "ticker:TWO",
    }


def test_completed_existing_review_frees_a_slot_before_new_first_staged_additions(monkeypatch):
    monkeypatch.setattr(dash_research, "MAX_NOTES", 2)
    run(
        [
            turn(("expand", {"node": "ticker:ONE"})),
            turn(
                ("remember_thesis", thesis("ticker:ONE")),
                ("reply", {"text": "[review:ticker:ONE]"}),
            ),
        ],
        automatic_reviews=True,
    )
    result, _ = run(
        [
            turn(
                ("expand", {"node": "ticker:THREE"}),
                ("expand", {"node": "ticker:FOUR"}),
                ("expand", {"node": "ticker:ONE"}),
            ),
            turn(
                ("remember_thesis", thesis("ticker:THREE")),
                ("remember_thesis", thesis("ticker:FOUR")),
                (
                    "finish_review",
                    {"node": "ticker:ONE", "outcome": "retired", "reason": "Review complete."},
                ),
            ),
            turn(("reply", {"text": "[review:ticker:THREE]\n[review:ticker:FOUR]"})),
        ],
        job_key="new-first",
        automatic_reviews=True,
        at_override=NOW + timedelta(hours=1),
    )
    assert result["action"] == "reply"
    assert set(result["review_schedule"]) == {"ticker:THREE", "ticker:FOUR"}
    assert {note["node"] for note in dash_research.notebook(SCOPE, NOW)} == {
        "ticker:THREE",
        "ticker:FOUR",
    }


def test_expanded_schedule_must_fit_before_reply_is_accepted():
    result, requests = run(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("remember_thesis", thesis())),
            turn(("reply", {"text": "x" * 3450 + "\n[review:ticker:TEST]"})),
            turn(("reply", {"text": "[review:ticker:TEST]"})),
        ],
        automatic_reviews=True,
    )
    assert "Shorten" in requests[3]["messages"][-1]["content"]
    assert "11:00" not in result["text"]
    assert "12:00 EST" in result["text"]


def test_legacy_offset_and_naive_due_times_are_compared_as_utc():
    note = {"node": "ticker:TEST", "status": "watching", "review_at": "2026-11-06T12:00:00-05:00"}
    assert dash_research.due_notes([note], NOW + timedelta(minutes=59)) == []
    assert dash_research.due_notes([note], NOW + timedelta(hours=1)) == [note]
    naive = {**note, "review_at": "2026-11-06T17:00:00"}
    assert dash_research.due_notes([naive], NOW + timedelta(hours=1)) == [naive]


def test_due_review_runs_while_a_prior_finding_waits_for_the_post_gap(monkeypatch):
    sent = []
    desk(monkeypatch, lambda *args, **kwargs: sent.append(args[2]) or 42)
    dash_research.save_thesis(
        SCOPE, thesis(), {"ticker:TEST": {"price": 1}}, NOW - timedelta(hours=2)
    )
    with connection() as database:
        dash_direction.finish(database, SCOPE, None, NOW, explored=True)
        enqueue_cards(
            database,
            "-100",
            [{"kind": "dash_research", "subject": "sent", "text": "Earlier finding."}],
            at=NOW - timedelta(minutes=10),
        )
        database.execute("UPDATE telegram_outbox SET status='sent',message_id=1")
        enqueue_cards(
            database,
            "-100",
            [{"kind": "dash_research", "subject": "waiting", "text": "Queued finding."}],
            at=NOW - timedelta(minutes=5),
        )
    responses = iter(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(
                (
                    "finish_review",
                    {"node": "ticker:TEST", "outcome": "reviewed_quiet", "reason": "Unchanged."},
                ),
                ("hold", {"why": "No new finding."}),
            ),
        ]
    )
    monkeypatch.setattr(main, "_telegram_chat_completion", lambda body: next(responses))
    monkeypatch.setattr(main, "dash_expand", lambda node: {"price": 1})
    result = main.post_dash_desk_note(at=NOW, scheduled_only=True)
    assert result["status"] == "held" and result["researched"] is True
    assert sent == []
    assert dash_research.notebook(SCOPE, NOW)[0]["reviewed_at"] == NOW.isoformat()
    later = main.post_dash_desk_note(at=NOW + timedelta(minutes=45), scheduled_only=True)
    assert later["status"] == "sent" and sent == ["Queued finding."]


def test_worker_polls_due_checks_each_minute_without_restarting_ordinary_research(monkeypatch):
    instant = [NOW]
    calls, sleeps = [], []
    monkeypatch.setattr(main, "DASH_DESK_NOTES_ENABLED", True)
    monkeypatch.setattr(main, "DASH_DESK_NOTE_SECONDS", 3600)
    monkeypatch.setattr(main, "now", lambda: instant[0])
    monkeypatch.setattr(main, "worker_state", lambda *args: None)

    def post(*, scheduled_only=False):
        calls.append(scheduled_only)
        return {"status": "quiet" if scheduled_only else "held"}

    async def in_pool(fn, **kwargs):
        return fn(**kwargs)

    async def sleep(seconds):
        sleeps.append(seconds)
        instant[0] += timedelta(seconds=seconds)
        if len(sleeps) == 5:
            raise asyncio.CancelledError

    monkeypatch.setattr(main, "post_dash_desk_note", post)
    monkeypatch.setattr(main, "run_in_threadpool", in_pool)
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.dash_desk_note_worker())
    assert calls == [False, True, True, True]
    assert sleeps == [60] * 5
