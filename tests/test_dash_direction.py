from __future__ import annotations

import json
from datetime import timedelta

import pytest

from runner_web import dash_direction as direction
from runner_web import dash_jobs, db, main
from runner_web.db import connection, init_db
from tests.test_dash_followthrough import desk
from tests.test_dash_research import NOW, SCOPE, run, turn


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "direction.db")
    init_db()


def agenda(**changes):
    return {
        "voice": "Warm, dry humor. Get to the interesting part.",
        "focus": "I want to understand why strong volume fades after a reopen.",
        "question": "Which recent reopen held its range, and what made it different?",
        "review_minutes": 60,
        **changes,
    }


def test_dash_writes_his_own_voice_and_carries_it_into_the_next_turn():
    result, _ = run(
        [turn(("set_direction", agenda()), ("reply", {"text": "I have a question to chase."}))]
    )
    assert result["action"] == "reply"
    saved = direction.read(SCOPE)
    assert saved["voice"] == agenda()["voice"]
    assert saved["question"] == agenda()["question"]
    assert direction.read("chat:-100:topic:7") == {}
    assert direction.read("chat:-101:topic:0") == {}
    _, requests = run([turn(("reply", {"text": "Back to the reopen."}))])
    assert json.loads(requests[0]["messages"][1]["content"])["your_direction"] == saved
    assert saved["voice"] not in requests[0]["messages"][0]["content"]


@pytest.mark.parametrize(
    "changes",
    [
        {"voice": ""},
        {"voice": "v" * 501},
        {"focus": "f" * 701},
        {"question": "q" * 501},
        {"question": []},
        {"review_minutes": True},
        {"review_minutes": 29},
        {"review_minutes": 1441},
    ],
)
def test_direction_requires_usable_notes_and_a_bounded_review_time(changes):
    assert "error" in direction.propose(agenda(**changes), NOW)


def test_quiet_research_follows_his_agenda_and_sets_the_next_visit(monkeypatch):
    desk(monkeypatch, lambda *a, **k: pytest.fail("This finding stays at the desk"))
    monkeypatch.setattr(main, "DASH_DECISIONS_ENABLED", True)
    monkeypatch.setattr(main, "_desk_note_gate", lambda *a: pytest.fail("Research is due"))
    with connection() as database:
        direction.finish(database, SCOPE, direction.propose(agenda(), NOW)["direction"], NOW)
    assert main.post_dash_desk_note(at=NOW)["status"] == "quiet"
    calls = []
    requests = []
    monkeypatch.setattr(main, "dash_expand", lambda node: calls.append(node) or {"known": True})
    responses = iter(
        [
            turn(("expand", {"node": "halts"})),
            turn(("expand", {"node": "ticker:TEST"})),
            turn(
                (
                    "set_direction",
                    agenda(question="Will the range hold tomorrow?", review_minutes=120),
                ),
                ("hold", {"why": "Worth another look after fresh volume arrives."}),
            ),
        ]
    )

    def complete(body):
        requests.append(body)
        return next(responses)

    monkeypatch.setattr(main, "_telegram_chat_completion", complete)
    later = NOW + timedelta(hours=1)
    assert main.post_dash_desk_note(at=later)["status"] == "held"
    assert calls == ["halts", "ticker:TEST"]
    context = json.loads(requests[0]["messages"][1]["content"])
    assert context["your_direction"]["question"] == agenda()["question"]
    assert context["world"]["self_directed_review"] is True
    assert direction.read(SCOPE)["question"] == "Will the range hold tomorrow?"
    assert direction.due(SCOPE, later + timedelta(minutes=119)) is False
    assert direction.due(SCOPE, later + timedelta(minutes=120)) is True


def test_first_quiet_tick_invites_exploration_then_waits_six_hours(monkeypatch):
    desk(monkeypatch, lambda *a, **k: pytest.fail("Quiet visit"))
    monkeypatch.setattr(
        main, "_telegram_chat_completion", lambda body: turn(("hold", {"why": "Resting."}))
    )
    assert main.post_dash_desk_note(at=NOW)["status"] == "held"
    assert main.post_dash_desk_note(at=NOW + timedelta(hours=5))["status"] == "quiet"
    assert direction.due(SCOPE, NOW + timedelta(hours=6)) is True


def test_direction_resumes_after_restart_and_commits_with_the_result():
    with pytest.raises(StopIteration):
        run([turn(("set_direction", agenda()))], job_key="direction-restart")
    assert direction.read(SCOPE) == {}
    _, requests = run(
        [turn(("hold", {"why": "Next question saved."}))],
        job_key="direction-restart",
        at_override=NOW + timedelta(minutes=1),
    )
    assert (
        json.loads(requests[0]["messages"][1]["content"])["your_direction"]["voice"]
        == agenda()["voice"]
    )
    assert direction.read(SCOPE)["voice"] == agenda()["voice"]
    run([], job_key="direction-restart", at_override=NOW + timedelta(hours=1))
    assert direction.read(SCOPE)["updated_at"] == NOW.isoformat()


def test_failed_outbox_commit_keeps_direction_and_schedule_uncommitted(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("queue")

    monkeypatch.setattr(dash_jobs, "enqueue_cards", fail)
    with pytest.raises(RuntimeError, match="queue"):
        run(
            [
                turn(("expand", {"node": "halts"})),
                turn(("set_direction", agenda()), ("reply", {"text": "A useful finding."})),
            ],
            job_key="direction-atomic",
            job_chat_id=-100,
            proactive=True,
        )
    assert direction.read(SCOPE) == {}
    assert direction.due(SCOPE, NOW) is True


def test_old_resumed_work_preserves_the_newer_direction():
    with pytest.raises(StopIteration):
        run([turn(("set_direction", agenda()))], job_key="old-direction")
    later = NOW + timedelta(minutes=2)
    run(
        [
            turn(
                ("set_direction", agenda(voice="Spare and thoughtful.")),
                ("reply", {"text": "A fresh angle."}),
            )
        ],
        at_override=later,
    )
    _, requests = run(
        [turn(("hold", {"why": "Ready."}))],
        job_key="old-direction",
        at_override=later + timedelta(minutes=1),
    )
    assert direction.read(SCOPE)["voice"] == "Spare and thoughtful."
    assert (
        json.loads(requests[0]["messages"][1]["content"])["your_direction"]["voice"]
        == "Spare and thoughtful."
    )


def test_a_saved_question_still_needs_evidence_before_a_public_finding():
    result, requests = run(
        [
            turn(("set_direction", agenda()), ("reply", {"text": "A claimed discovery."})),
            turn(("hold", {"why": "The evidence needs a closer look."})),
        ],
        proactive=True,
    )
    assert result["action"] == "hold"
    assert any(
        "Expand relevant evidence" in str(message.get("content", ""))
        for message in requests[-1]["messages"]
    )


def test_direction_tool_respects_the_research_budget(monkeypatch):
    from runner_web import dash_research

    monkeypatch.setattr(dash_research, "MAX_TOOLS", 1)
    result, requests = run(
        [
            turn(("set_direction", agenda())),
            turn(("set_direction", agenda(voice="New voice.")), ("hold", {"why": "Done."})),
        ]
    )
    assert result["action"] == "hold"
    assert direction.read(SCOPE)["voice"] == agenda()["voice"]
    assert {item["function"]["name"] for item in requests[-1]["tools"]} == {"reply", "hold"}
