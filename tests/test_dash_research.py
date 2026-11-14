from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from runner_web import dash_direction, db
from runner_web import dash_research as research
from runner_web.db import connection, init_db
from runner_web.telegram_chat import TOOL_SCHEMA

NOW = datetime(2026, 11, 6, 16, tzinfo=UTC)
SCOPE = research.research_scope(-100)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "research.db")
    init_db()


def thesis(node="ticker:TEST", **changes):
    return {
        "node": node,
        "thesis": "The bounce looks fragile because volume is fading.",
        "confidence": "medium",
        "counter_evidence": "The price is still above the opening range.",
        "change_my_mind": "Fresh volume with a sustained break above that range.",
        "next_check": "Compare price and relative volume after the next scan.",
        "review_minutes": 60,
        "status": "watching",
        **changes,
    }


def turn(*actions):
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": f"call-{index}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args),
                            },
                        }
                        for index, (name, args) in enumerate(actions)
                    ],
                }
            }
        ]
    }


def run(script, *, expand=None, evidence=None, **kwargs):
    requests = []
    responses = iter(script)

    def complete(body):
        requests.append(copy.deepcopy(body))
        return next(responses)

    result = research.run_research(
        context=kwargs.pop("context", {}),
        persona="Dash",
        tools=list(TOOL_SCHEMA),
        complete=complete,
        model="test-model",
        expand=expand or (lambda node: {"known": True, "price": 2}),
        unbacked=kwargs.pop("unbacked", lambda text, evidence: []),
        evidence=evidence if evidence is not None else {},
        scope=SCOPE,
        at=kwargs.pop("at_override", NOW),
        **kwargs,
    )
    return result, requests


def test_notes_keep_the_view_evidence_and_a_review_time():
    evidence = {"ticker:TEST": {"known": True, "price": 2, "observed_at": NOW.isoformat()}}
    result = research.save_thesis(SCOPE, thesis(), evidence, NOW)
    assert result["saved"] is True
    notes = research.notebook(SCOPE, NOW)
    assert notes[0]["confidence"] == "medium"
    assert notes[0]["change_my_mind"] == thesis()["change_my_mind"]
    assert json.loads(notes[0]["evidence"])["price"] == 2
    assert research.due_notes(notes, NOW) == []
    assert research.due_notes(notes, NOW + timedelta(hours=1)) == notes
    assert research.notebook(research.research_scope(-101), NOW) == []
    assert research.notebook(research.research_scope(-100, 7), NOW) == []
    assert research.notebook(SCOPE, NOW + timedelta(days=15)) == []


def test_a_revised_view_keeps_its_previous_take_and_can_close():
    evidence = {"ticker:TEST": {"known": True, "price": 2}}
    research.save_thesis(SCOPE, thesis(), evidence, NOW)
    later = NOW + timedelta(hours=1)
    research.save_thesis(
        SCOPE,
        thesis(thesis="Fresh volume supports the breakout.", status="closed"),
        evidence,
        later,
    )
    notes = research.notebook(SCOPE, later)
    assert len(notes) == 1
    assert notes[0]["previous_view"]["thesis"] == thesis()["thesis"]
    assert notes[0]["thesis"] == "Fresh volume supports the breakout."
    assert research.due_notes(notes, later + timedelta(days=1)) == []


@pytest.mark.parametrize(
    "changes,evidence",
    [
        ({}, {}),
        ({}, {"ticker:TEST": {"known": False}}),
        ({}, {"ticker:TEST": {"error": "unavailable"}}),
        ({"node": "https://example.com"}, {}),
        ({"review_minutes": True}, {"ticker:TEST": {"price": 2}}),
        ({"review_minutes": 29}, {"ticker:TEST": {"price": 2}}),
        ({"review_minutes": 1441}, {"ticker:TEST": {"price": 2}}),
        ({"confidence": "certain"}, {"ticker:TEST": {"price": 2}}),
        ({"status": "maybe"}, {"ticker:TEST": {"price": 2}}),
        ({"change_my_mind": ""}, {"ticker:TEST": {"price": 2}}),
    ],
)
def test_a_saved_view_requires_evidence_and_complete_fields(changes, evidence):
    assert "error" in research.save_thesis(SCOPE, thesis(**changes), evidence, NOW)
    assert research.notebook(SCOPE, NOW) == []


def test_notebooks_keep_twelve_recent_notes_and_clean_expired_rows():
    for index in range(15):
        node = f"coin:address{index}"
        research.save_thesis(
            SCOPE, thesis(node), {node: {"price": index}}, NOW + timedelta(seconds=index)
        )
    notes = research.notebook(SCOPE, NOW + timedelta(minutes=1))
    assert len(notes) == 12
    assert {n["node"] for n in notes} == {f"coin:address{i}" for i in range(3, 15)}
    research.save_thesis(SCOPE, thesis(), {"ticker:TEST": {"price": 2}}, NOW + timedelta(days=15))
    with connection() as database:
        assert (
            database.execute(
                "SELECT COUNT(*) FROM worker_state WHERE key LIKE 'dash_thesis:%'"
            ).fetchone()[0]
            == 1
        )


def test_an_analyst_checks_two_sources_then_saves_his_own_take():
    reads = []
    answer = "My take: the bounce is fragile. Medium confidence. Fresh volume would change my view."
    result, requests = run(
        [
            turn(("expand", {"node": "ticker:TEST"}), ("expand", {"node": "sector:technology"})),
            turn(("remember_thesis", thesis()), ("reply", {"text": answer})),
        ],
        expand=lambda node: reads.append(node) or {"known": True, "price": 2},
    )
    assert result == {"action": "reply", "text": answer}
    assert reads == ["ticker:TEST", "sector:technology"]
    tool_results = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["call-0", "call-1"]
    assert research.notebook(SCOPE, NOW)[0]["thesis"] == thesis()["thesis"]
    _, followup = run([turn(("reply", {"text": "I will check the volume next."}))])
    context = json.loads(followup[0]["messages"][1]["content"])
    assert context["research_notebook"][0]["thesis"] == thesis()["thesis"]


def test_repeated_reads_share_one_lookup_and_coin_case_is_preserved():
    reads = []
    node = "coin:AbCdEf123"
    result, requests = run(
        [
            turn(("expand", {"node": node}), ("expand", {"node": node})),
            turn(("reply", {"text": "The evidence supports a cautious view."})),
        ],
        expand=lambda key: reads.append(key) or {"known": True},
    )
    assert result["action"] == "reply"
    assert reads == [node]
    assert len([m for m in requests[1]["messages"] if m["role"] == "tool"]) == 2


def test_a_source_failure_leaves_other_research_available():
    def expand(node):
        if node == "events":
            raise TimeoutError("source unavailable")
        return {"count": 2}

    result, requests = run(
        [
            turn(("expand", {"node": "events"}), ("expand", {"node": "halts"})),
            turn(("reply", {"text": "My view is tentative while the news check is pending."})),
        ],
        expand=expand,
    )
    assert result["action"] == "reply"
    assert "Lookup failed" in requests[1]["messages"][3]["content"]


def test_proactive_notes_require_research_before_a_take():
    result, requests = run(
        [
            turn(("reply", {"text": "The market moved."})),
            turn(("expand", {"node": "board"})),
            turn(("reply", {"text": "My take: volume leaves this move fragile."})),
        ],
        proactive=True,
        evidence={"world": {}},
    )
    assert len(requests) == 3
    assert result["text"].startswith("My take")


def test_a_due_thesis_gets_fresh_evidence_and_an_explicit_review():
    research.save_thesis(SCOPE, thesis(), {"ticker:TEST": {"price": 1}}, NOW - timedelta(hours=2))
    new = thesis(thesis="The bounce gained support from fresh volume.", confidence="high")
    result, requests = run(
        [
            turn(("hold", {"why": "same view"})),
            turn(("expand", {"node": "ticker:TEST"})),
            turn(
                ("remember_thesis", new),
                ("reply", {"text": "I changed my view as volume returned."}),
            ),
        ],
        proactive=True,
        evidence={"world": {}},
    )
    assert result["text"] == "I changed my view as volume returned."
    assert "Review the due theses" in requests[1]["messages"][-1]["content"]
    note = research.notebook(SCOPE, NOW)[0]
    assert note["previous_view"]["thesis"] == thesis()["thesis"]
    assert json.loads(note["evidence"])["price"] == 2
    assert research.due_notes([note], NOW) == []


def test_the_last_round_is_reserved_for_an_answer(monkeypatch):
    monkeypatch.setattr(research, "MAX_ROUNDS", 2)
    result, requests = run(
        [
            turn(("expand", {"node": "board"})),
            turn(("reply", {"text": "My take is tentative because the sample is small."})),
        ]
    )
    assert result["action"] == "reply"
    assert {t["function"]["name"] for t in requests[-1]["tools"]} == {"reply", "hold"}


def test_the_tool_budget_covers_all_calls_in_a_batch(monkeypatch):
    monkeypatch.setattr(research, "MAX_TOOLS", 2)
    reads = []
    result, requests = run(
        [
            turn(*[("expand", {"node": node}) for node in ("board", "events", "halts")]),
            turn(("reply", {"text": "The board and events support a cautious view."})),
        ],
        expand=lambda node: reads.append(node) or {"count": 1},
    )
    assert result["action"] == "reply"
    assert reads == ["board", "events"]
    assert "Research budget reached" in requests[1]["messages"][-2]["content"]


def test_deadline_stops_starting_more_model_calls(monkeypatch):
    clock = iter([0, 0, 0, 100])
    monkeypatch.setattr(research.time, "monotonic", lambda: next(clock))
    result, requests = run([turn(("expand", {"node": "board"}))])
    assert len(requests) == 1
    assert result["action"] == "reply"
    assert "try again" in result["text"]


def test_public_actions_require_a_ticker_lookup():
    acted = []
    result, requests = run(
        [
            turn(("make_call", {"ticker": "TEST"})),
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("make_call", {"ticker": "TEST"})),
            turn(("reply", {"text": "I opened a paper Call after the evidence check."})),
        ],
        act=lambda name, args: acted.append(name) or {"ok": True},
    )
    assert result["action"] == "reply"
    assert acted == ["make_call"]
    assert "Expand the ticker" in requests[1]["messages"][-1]["content"]


def test_a_terminal_action_finishes_the_batch():
    acted = []
    result, _ = run(
        [
            turn(
                ("reply", {"text": "I am watching the next scan."}),
                ("make_call", {"ticker": "TEST"}),
            ),
        ],
        act=lambda name, args: acted.append(name),
    )
    assert result["action"] == "reply"
    assert acted == []


def test_repeated_unsupported_claims_receive_a_safe_error():
    result, requests = run(
        [
            turn(("reply", {"text": "Unsupported claim."})),
            turn(("reply", {"text": "Another unsupported claim."})),
        ],
        unbacked=lambda text, evidence: ["TEST"],
    )
    assert len(requests) == 2
    assert "try again" in result["text"]


def test_a_quiet_world_still_reviews_due_room_ideas(monkeypatch):
    from runner_web import main

    research.save_thesis(SCOPE, thesis(), {"ticker:TEST": {"price": 1}}, NOW - timedelta(hours=2))
    monkeypatch.setattr(main, "DASH_DESK_NOTES_ENABLED", True)
    monkeypatch.setattr(main, "DASH_DECISIONS_ENABLED", True)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("TELEGRAM_API_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    monkeypatch.setattr(main, "telegram_room_chat_id", lambda: -100)
    monkeypatch.setattr(main, "dash_world", lambda at: {"changes": {"any": False}})
    monkeypatch.setattr(
        main, "_desk_note_gate", lambda *args: pytest.fail("Due work needs research")
    )
    monkeypatch.setattr(main, "dash_expand", lambda node: {"known": True, "price": 2})
    script = iter(
        [
            turn(("expand", {"node": "ticker:TEST"})),
            turn(("remember_thesis", thesis()), ("hold", {"why": "My view is unchanged."})),
        ]
    )
    monkeypatch.setattr(main, "_telegram_chat_completion", lambda body: next(script))
    monkeypatch.setattr(
        main, "send_telegram_reply", lambda *args, **kwargs: pytest.fail("Quiet review")
    )
    assert main.post_dash_desk_note(at=NOW)["status"] == "held"
    assert research.notebook(SCOPE, NOW)[0]["reviewed_at"] == NOW.isoformat()
    assert main.post_dash_desk_note(at=NOW + timedelta(minutes=5))["status"] == "quiet"
    monkeypatch.setattr(main, "telegram_room_chat_id", lambda: -101)
    with connection() as database:
        dash_direction.finish(database, research.research_scope(-101), None, NOW, explored=True)
    assert main.post_dash_desk_note(at=NOW + timedelta(hours=2))["status"] == "quiet"


def test_same_tick_saves_keep_the_latest_note():
    for index in range(20):
        node = f"coin:sameTick{index}"
        assert research.save_thesis(SCOPE, thesis(node), {node: {"known": True}}, NOW)["saved"]
        assert node in {note["node"] for note in research.notebook(SCOPE, NOW)}
    assert len(research.notebook(SCOPE, NOW)) == research.MAX_NOTES


def test_an_unchanged_review_preserves_the_last_change_of_mind():
    source = {"ticker:TEST": {"price": 2}}
    research.save_thesis(SCOPE, thesis(), source, NOW)
    revised = thesis(thesis="Fresh volume supports the move.")
    research.save_thesis(SCOPE, revised, source, NOW + timedelta(hours=1))
    research.save_thesis(SCOPE, revised, source, NOW + timedelta(hours=2))
    assert (
        research.notebook(SCOPE, NOW + timedelta(hours=2))[0]["previous_view"]["thesis"]
        == thesis()["thesis"]
    )


def test_a_near_deadline_turn_goes_straight_to_its_answer(monkeypatch):
    clock = iter([0, 50])
    monkeypatch.setattr(research.time, "monotonic", lambda: next(clock))
    result, requests = run([turn(("reply", {"text": "The evidence leaves my view tentative."}))])
    assert result["action"] == "reply"
    assert {tool["function"]["name"] for tool in requests[0]["tools"]} == {"reply", "hold"}


def test_a_long_reply_keeps_whole_addresses():
    address = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"
    prefix = "Evidence. " * 348
    result, _ = run([turn(("reply", {"text": prefix + address}))])
    assert result["text"] == prefix.rstrip()


def test_bad_tool_arguments_and_unknown_tools_get_feedback():
    result, requests = run(
        [
            turn(("expand", []), ("unknown", {}), ("expand", {"node": "shell:ls"})),
            turn(("reply", {"text": "My view needs a better source."})),
        ]
    )
    assert result["action"] == "reply"
    replies = [json.loads(m["content"]) for m in requests[1]["messages"] if m["role"] == "tool"]
    assert len(replies) == 3
    assert all("error" in reply for reply in replies)
