"""Dash's decision-model path: the client, the policy, and the turn it drives.

No live call is made. Fixtures follow the response shape in OpenRouter's
Decisions API documentation (typesafe/jev-1.13).
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from runner_web import db
from runner_web import decisions as dec
from runner_web import telegram_chat as chat
from runner_web.dash_decisions import (
    DESK_NOTE_QUESTIONS,
    TURN_QUESTIONS,
    choose_turn,
    desk_note_worth_it,
    turn_state,
)
from runner_web.db import connection, init_db

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
CHAT = -1002222222222
ALICE = 4242
BOT_ID = 7849797828
BOT = "DashRATiBot"
CA = "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "decisions.db")
    init_db()


# ---------------------------------------------------------------------------
# Recorded-shape fixtures
# ---------------------------------------------------------------------------

DOC_EXAMPLE = {
    "id": "dec-123",
    "model": "typesafe/jev-1.13",
    "provider": "TypeSafe",
    "answers": {
        "is_bug": {"type": "noul", "noul": 0.96},
        "team": {
            "type": "choice",
            "choice": "payments",
            "confidence": 0.67,
            "probabilities": {"payments": 0.78, "frontend": 0.22, "account": 0},
        },
        "urgency": {
            "type": "score",
            "score": 1.99,
            "confidence": 0.99,
            "probabilities": {"0": 0, "1": 0, "2": 1},
            "legend": {"0": "later", "1": "this week", "2": "now"},
        },
    },
    "usage": {"input_tokens": 120, "output_tokens": 0, "cost": 0.00012},
}
DOC_QUESTIONS = {
    "is_bug": dec.noul("Is it a bug?", "yes", "no"),
    "team": dec.choice("Which team?", {"payments": "p", "frontend": "f", "account": "a"}),
    "urgency": dec.score("How urgent?", ["later", "this week", "now"]),
}


def _choice(option: str, p: float, options: list[str]) -> dict[str, Any]:
    rest = (1 - p) / max(1, len(options) - 1)
    return {
        "type": "choice",
        "choice": option,
        "confidence": p,
        "probabilities": {name: (p if name == option else rest) for name in options},
    }


def turn_payload(
    *,
    engage: float = 0.9,
    stop: float = 0.02,
    action: tuple[str, float] = ("reply", 0.9),
    needs: tuple[str, float] = ("none", 0.9),
    intent: tuple[str, float] = ("none", 0.95),
    cost: float = 0.0001,
) -> dict[str, Any]:
    return {
        "id": "dec-1",
        "model": "typesafe/jev-1.13",
        "provider": "TypeSafe",
        "answers": {
            "should_engage": {"type": "noul", "noul": engage},
            "stop_requested": {"type": "noul", "noul": stop},
            "action": _choice(*action, ["reply", "react", "hold"]),
            "needs": _choice(*needs, list(TURN_QUESTIONS["needs"]["criteria"])),
            "call_intent": _choice(*intent, list(TURN_QUESTIONS["call_intent"]["criteria"])),
        },
        "usage": {"input_tokens": 300, "output_tokens": 0, "cost": cost},
    }


def turn_answers(**kwargs):
    return dec.parse_response(turn_payload(**kwargs), TURN_QUESTIONS).answers


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


def test_the_parser_reads_every_answer_type_from_the_documented_example():
    decision = dec.parse_response(DOC_EXAMPLE, DOC_QUESTIONS)

    assert decision.id == "dec-123"
    assert decision.cost == pytest.approx(0.00012)
    assert decision.answers["is_bug"].type == "noul"
    assert decision.answers["is_bug"].value == pytest.approx(0.96)
    team = decision.answers["team"]
    assert (team.value, team.confidence) == ("payments", 0.67)
    assert team.probability("payments") == pytest.approx(0.78)
    assert team.probability("account") == 0
    urgency = decision.answers["urgency"]
    assert urgency.value == pytest.approx(1.99)
    assert urgency.probability("2") == 1
    assert decision.answers_json()["team"]["probabilities"]["frontend"] == 0.22


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("answers"),
        lambda p: p["answers"].pop("team"),
        lambda p: p["answers"]["is_bug"].update(noul="high"),
        lambda p: p["answers"]["is_bug"].update(noul=1.4),
        lambda p: p["answers"]["is_bug"].update(type="choice"),
        lambda p: p["answers"]["team"].update(choice="marketing"),
        lambda p: p["answers"]["team"]["probabilities"].update(marketing=0.1),
        lambda p: p["answers"]["team"].update(probabilities=None),
        lambda p: p["answers"]["urgency"].update(score=7),
        lambda p: p["answers"]["urgency"].update(score=None),
        lambda p: p["answers"]["urgency"]["probabilities"].update({"9": 0.1}),
        lambda p: p["answers"].update(team="payments"),
    ],
)
def test_a_malformed_answer_is_refused_whole(mutate):
    payload = json.loads(json.dumps(DOC_EXAMPLE))
    mutate(payload)

    with pytest.raises(dec.DecisionError) as raised:
        dec.parse_response(payload, DOC_QUESTIONS)
    assert raised.value.kind == "malformed"


def test_a_response_that_is_not_an_object_is_refused():
    with pytest.raises(dec.DecisionError):
        dec.parse_response(["answers"], DOC_QUESTIONS)


def test_the_request_carries_only_documented_fields(monkeypatch):
    monkeypatch.delenv("DASH_DECISION_MODEL", raising=False)
    sent: dict[str, Any] = {}

    def post(body, **kwargs):
        sent.update(body=body, **kwargs)
        return DOC_EXAMPLE

    dec.decide({"ticket": "x"}, DOC_QUESTIONS, api_key="k", provider={"zdr": True}, post=post)

    assert set(sent["body"]) == {"model", "state", "questions", "provider"}
    assert sent["body"]["model"] == "typesafe/jev-1.13"
    assert sent["body"]["provider"] == {"zdr": True}
    assert sent["body"]["questions"]["team"]["type"] == "choice"
    assert sent["api_key"] == "k"


def test_the_decision_model_can_be_overridden(monkeypatch):
    monkeypatch.setenv("DASH_DECISION_MODEL", "typesafe/jev-2")
    assert dec.decision_model() == "typesafe/jev-2"
    body = dec.build_request({}, DOC_QUESTIONS)
    assert body["model"] == "typesafe/jev-2"
    assert "provider" not in body


def test_an_unknown_question_type_is_a_programming_error():
    with pytest.raises(ValueError):
        dec.build_request({}, {"q": {"type": "text", "instructions": "?"}})


def test_no_api_key_fails_before_any_request():
    with pytest.raises(dec.DecisionError) as raised:
        dec.decide({}, DOC_QUESTIONS, api_key="", post=lambda *a, **k: pytest.fail("posted"))
    assert raised.value.status == 401


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 413, 429, 500, 502, 503])
def test_an_http_error_becomes_a_decision_error_with_its_status(monkeypatch, status):
    def urlopen(request, timeout):
        assert request.full_url == dec.DECISIONS_URL
        assert timeout == dec.TIMEOUT_SECONDS
        raise urllib.error.HTTPError(
            request.full_url, status, "err", {}, io.BytesIO(b'{"error":"no credits"}')
        )

    monkeypatch.setattr(dec.urllib.request, "urlopen", urlopen)

    with pytest.raises(dec.DecisionError) as raised:
        dec.decide({}, DOC_QUESTIONS, api_key="k")
    assert raised.value.kind == "http"
    assert raised.value.status == status


def test_a_timeout_becomes_a_network_error(monkeypatch):
    def urlopen(request, timeout):
        raise TimeoutError

    monkeypatch.setattr(dec.urllib.request, "urlopen", urlopen)

    with pytest.raises(dec.DecisionError) as raised:
        dec.decide({}, DOC_QUESTIONS, api_key="k")
    assert raised.value.kind == "network"


def test_a_body_that_is_not_json_is_malformed(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(dec.urllib.request, "urlopen", lambda request, timeout: Response(b"<html>"))

    with pytest.raises(dec.DecisionError) as raised:
        dec.decide({}, DOC_QUESTIONS, api_key="k")
    assert raised.value.kind == "malformed"


def test_a_good_body_round_trips_through_urllib(monkeypatch):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen = {}

    def urlopen(request, timeout):
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data)
        return Response(json.dumps(DOC_EXAMPLE).encode())

    monkeypatch.setattr(dec.urllib.request, "urlopen", urlopen)

    decision = dec.decide({"a": 1}, DOC_QUESTIONS, api_key="secret", referer="https://x.test")

    assert decision.answers["team"].value == "payments"
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["body"]["state"] == {"a": 1}


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------

BUDGET = {"can_call": True, "can_comment": True, "calls_left": 2}
NO_BUDGET = {"can_call": False, "can_comment": False, "calls_left": 0}


def _plan(addressed=True, budget=BUDGET, tickers=("MSGM",), addresses=(), **answers):
    return choose_turn(
        turn_answers(**answers),
        addressed=addressed,
        budget=budget,
        tickers=list(tickers),
        addresses=list(addresses),
        confidence=0.6,
        mutation_confidence=0.8,
    )


@pytest.mark.parametrize(
    ("kwargs", "action", "reason", "mute"),
    [
        ({}, "reply", "decided", False),
        ({"stop": 0.9}, "hold", "stop_requested", True),
        ({"stop": 0.9, "action": ("reply", 0.99)}, "hold", "stop_requested", True),
        ({"stop": 0.59}, "reply", "decided", False),
        ({"action": ("reply", 0.5)}, "react", "low_confidence", False),
        ({"action": ("reply", 0.5), "addressed": False}, "hold", "low_confidence", False),
        ({"action": ("react", 0.8)}, "react", "decided", False),
        ({"action": ("hold", 0.8)}, "hold", "decided", False),
        ({"addressed": False, "engage": 0.3}, "hold", "not_engaging", False),
        # Addressed messages are not dropped for a low should_engage.
        ({"addressed": True, "engage": 0.1}, "reply", "decided", False),
    ],
)
def test_the_policy_maps_probabilities_to_one_action(kwargs, action, reason, mute):
    plan = _plan(**kwargs)

    assert (plan.action, plan.reason, plan.mute) == (action, reason, mute)
    if action != "reply":
        assert plan.node is None and plan.mutation is None


@pytest.mark.parametrize(
    ("needs", "tickers", "addresses", "node"),
    [
        (("board", 0.9), (), (), "board"),
        (("halts", 0.9), (), (), "halts"),
        (("board", 0.5), (), (), None),
        (("ticker", 0.9), ("MSGM",), (), "ticker:MSGM"),
        (("ticker", 0.9), (), (), None),
        (("coin", 0.9), (), (CA,), f"coin:{CA}"),
        (("coin", 0.9), (), (), None),
        (("none", 0.9), ("MSGM",), (), None),
    ],
)
def test_needs_becomes_an_expand_node_from_the_message_itself(needs, tickers, addresses, node):
    plan = _plan(needs=needs, tickers=tickers, addresses=addresses)

    assert plan.node == node


@pytest.mark.parametrize(
    ("intent", "budget", "tickers", "mutation", "reason"),
    [
        (("open_call", 0.9), BUDGET, ("MSGM",), "make_call", "decided"),
        (("open_call", 0.79), BUDGET, ("MSGM",), None, "decided"),
        (("open_call", 0.9), NO_BUDGET, ("MSGM",), None, "decided,no_calls_left"),
        (
            ("open_call", 0.9),
            {**BUDGET, "calls_left": 0},
            ("MSGM",),
            None,
            "decided,no_calls_left",
        ),
        (("open_call", 0.9), BUDGET, (), None, "decided,no_ticker_for_open_call"),
        (("close_call", 0.9), NO_BUDGET, ("MSGM",), "close_call", "decided"),
        (("comment", 0.9), BUDGET, ("MSGM",), "comment_on_ticker", "decided"),
        (("comment", 0.9), NO_BUDGET, ("MSGM",), None, "decided,no_comments_left"),
    ],
)
def test_calls_and_comments_need_the_higher_bar_and_budget(
    intent, budget, tickers, mutation, reason
):
    plan = _plan(intent=intent, budget=budget, tickers=tickers)

    assert plan.action == "reply"
    assert plan.mutation == mutation
    assert plan.reason == reason
    assert plan.ticker == ("MSGM" if mutation else None)


def test_no_mutation_without_a_reply():
    plan = _plan(action=("react", 0.9), intent=("open_call", 0.99))

    assert plan.action == "react"
    assert plan.mutation is None


def test_the_desk_note_speaks_only_above_the_score_and_confidence():
    def worth(position: float, sure: float) -> bool:
        answer = dec.Answer("score", position, sure, {"0": 0, "1": 0, "2": 1, "3": 0})
        return desk_note_worth_it(answer, min_score=2.0, confidence=0.6)

    assert worth(2.4, 0.9) is True
    assert worth(1.9, 0.9) is False
    assert worth(2.4, 0.4) is False


def test_the_turn_state_is_small_and_leaves_the_world_out():
    state = turn_state(
        speaker="Alice",
        text="x" * 5000,
        addressed=True,
        transcript=[{"who": "Bob", "said": "y" * 900}] * 10,
        recent_actions=[{"action": "reply", "detail": "hi"}] * 8,
        budget={"can_call": True, "can_comment": False, "calls_left": 2, "balance": 99},
        changes={"new_runners": 2, "any": True, "window_hours": 1},
        session="Regular session",
        tickers=["MSGM"],
        addresses=[],
    )

    assert len(state["message"]["text"]) == 800
    assert len(state["recent_lines"]) == 4
    assert len(state["recent_lines"][0]["said"]) == 200
    assert len(state["dash_recent_actions"]) == 3
    assert state["dash_budget"] == {"can_call": True, "can_comment": False, "calls_left": 2}
    assert state["changes"] == {"new_runners": 2, "any": True}
    assert len(json.dumps(state)) < 3000


def test_the_questions_are_well_formed():
    for questions in (TURN_QUESTIONS, DESK_NOTE_QUESTIONS):
        body = dec.build_request({}, questions)
        for question in body["questions"].values():
            assert question["instructions"]
            assert question["criteria"]
    assert set(TURN_QUESTIONS["action"]["criteria"]) == {"reply", "react", "hold"}
    assert set(TURN_QUESTIONS["call_intent"]["criteria"]) == {
        "none",
        "open_call",
        "close_call",
        "comment",
    }


# ---------------------------------------------------------------------------
# The turn, end to end through run_telegram_chat
# ---------------------------------------------------------------------------


def _update(text: str, *, update_id: int = 1, mention: bool = True) -> dict:
    body = f"@{BOT} {text}" if mention else text
    message: dict = {
        "message_id": 900 + update_id,
        "date": int(NOW.timestamp()),
        "chat": {"id": CHAT, "type": "supergroup"},
        "from": {"id": ALICE, "first_name": "Alice", "is_bot": False},
        "text": body,
    }
    if mention:
        message["entities"] = [{"type": "mention", "offset": 0, "length": len(BOT) + 1}]
    return {"update_id": update_id, "message": message}


class Room:
    def __init__(self) -> None:
        self.replies: list[str] = []
        self.reactions: list[str] = []
        self.states: list[Any] = []
        self.writes: list[dict[str, Any]] = []
        self.fallbacks = 0


@pytest.fixture
def room(monkeypatch):
    from runner_web import main as web_main
    from runner_web.telegram import TelegramConfig

    seen = Room()
    monkeypatch.setattr(web_main, "_telegram_identity", lambda: (BOT, BOT_ID))
    monkeypatch.setattr(
        web_main,
        "telegram_config_from_env",
        lambda: TelegramConfig(bot_token="token", chat_id=str(CHAT)),
    )
    monkeypatch.setattr(
        web_main,
        "send_telegram_reply",
        lambda config, chat_id, text, **_: seen.replies.append(text),
    )
    monkeypatch.setattr(
        web_main,
        "set_telegram_reaction",
        lambda config, chat_id, message_id, emoji: seen.reactions.append(emoji),
    )
    monkeypatch.setattr(web_main, "dash_budget", lambda at=None: dict(BUDGET))
    monkeypatch.setattr(web_main, "dash_recent_changes", lambda at=None: {"any": False})
    monkeypatch.setattr(
        web_main,
        "telegram_prefetch_for",
        lambda message, database: {"resolved_tickers": [], "looked_up": []},
    )

    def fallback(message, transcript):
        seen.fallbacks += 1
        return {"action": "reply", "text": "fallback words"}

    monkeypatch.setattr(web_main, "_generate_telegram_turn", fallback)

    def completion(body):
        assert "tools" not in body and "tool_choice" not in body
        seen.writes.append(body)
        return {"choices": [{"message": {"content": "chirp. quiet tape."}}]}

    monkeypatch.setattr(web_main, "_telegram_chat_completion", completion)
    return seen


def _decide_with(monkeypatch, room: Room, payload_or_error: Any) -> None:
    from runner_web import main as web_main

    def fake(state, questions):
        room.states.append(state)
        if isinstance(payload_or_error, Exception):
            raise payload_or_error
        return dec.parse_response(payload_or_error, questions)

    monkeypatch.setattr(web_main, "_dash_decide", fake)


def _run(text: str = "what is up", **kwargs) -> dict[str, int]:
    from runner_web import main as web_main

    with connection() as database:
        chat.record_update(database, _update(text, **kwargs), NOW)
    return web_main.run_telegram_chat(web_main._decide_telegram_turn, at=NOW)


def _logged() -> list[dict[str, Any]]:
    with connection() as database:
        return [dict(row) for row in database.execute("SELECT * FROM dash_decisions ORDER BY id")]


def test_a_reply_decision_writes_once_without_tools(room, monkeypatch):
    _decide_with(monkeypatch, room, turn_payload())

    result = _run()

    assert result["replied"] == 1
    assert room.replies == ["chirp. quiet tape."]
    assert len(room.writes) == 1
    assert room.fallbacks == 0
    state = room.states[0]
    assert state["message"]["addressed_to_dash"] is True
    assert "board" not in state and "memecoins" not in state
    row = _logged()[0]
    assert (row["update_id"], row["kind"], row["source"], row["action"]) == (
        1,
        "turn",
        "decision",
        "reply",
    )
    assert json.loads(row["answers_json"])["action"]["value"] == "reply"
    assert row["cost"] == pytest.approx(0.0001)


def test_react_and_hold_never_call_the_writer(room, monkeypatch):
    _decide_with(monkeypatch, room, turn_payload(action=("react", 0.9)))
    assert _run(update_id=1)["reacted"] == 1

    _decide_with(monkeypatch, room, turn_payload(action=("hold", 0.9)))
    assert _run("ok", update_id=2)["held"] == 1

    assert room.writes == []
    assert room.replies == []
    assert room.reactions == ["🐆"]
    assert [row["action"] for row in _logged()] == ["react", "hold"]


def test_an_unsure_decision_on_a_direct_question_reacts(room, monkeypatch):
    _decide_with(monkeypatch, room, turn_payload(action=("reply", 0.4)))

    assert _run()["reacted"] == 1
    assert room.writes == []
    assert room.reactions == ["👀"]


def test_a_stop_request_mutes_without_any_writing(room, monkeypatch):
    _decide_with(monkeypatch, room, turn_payload(stop=0.95))

    assert _run("stop talking")["held"] == 1

    assert room.writes == []
    with connection() as database:
        follow = chat.attention_for(
            database,
            chat.parse_update(_update("hey", update_id=2), bot_username=BOT, bot_id=BOT_ID),
            NOW + timedelta(minutes=1),
        )
    assert follow.reason == "muted"


@pytest.mark.parametrize(
    "error",
    [
        dec.DecisionError("http", "no credits", status=402),
        dec.DecisionError("network", "TimeoutError"),
        dec.DecisionError("malformed", "answer action is missing"),
        RuntimeError("boom"),
    ],
)
def test_a_failed_decision_falls_back_to_the_tool_loop(room, monkeypatch, error):
    _decide_with(monkeypatch, room, error)

    result = _run()

    assert result["replied"] == 1
    assert room.fallbacks == 1
    assert room.replies == ["fallback words"]
    row = _logged()[0]
    assert (row["source"], row["kind"]) == ("fallback", "turn")
    assert row["error"]
    with connection() as database:
        saved = database.execute(
            "SELECT value FROM worker_state WHERE key='dash_decision_last_error'"
        ).fetchone()
    assert saved and "turn" in saved["value"]


def test_an_unparsable_answer_falls_back(room, monkeypatch):
    payload = turn_payload()
    payload["answers"]["action"]["choice"] = "dance"
    _decide_with(monkeypatch, room, payload)

    assert _run()["replied"] == 1
    assert room.fallbacks == 1


def test_the_writer_gets_the_loaded_node(room, monkeypatch):
    from runner_web import main as web_main

    expanded: list[str] = []
    monkeypatch.setattr(
        web_main, "dash_expand", lambda node: expanded.append(node) or {"halts": ["MSGM"]}
    )
    _decide_with(monkeypatch, room, turn_payload(needs=("halts", 0.9)))

    _run("any halts?")

    assert expanded == ["halts"]
    context = json.loads(room.writes[0]["messages"][1]["content"])
    assert context["loaded"] == {"halts": {"halts": ["MSGM"]}}
    assert room.writes[0]["messages"][0]["content"] == chat.CHEETAH_PERSONA


def test_an_unbacked_ticker_gets_one_rewrite(room, monkeypatch):
    from runner_web import main as web_main

    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) "
            "VALUES(?,?,?,'NASDAQ',?)",
            (1, "NCPL", "Netcapital", NOW.isoformat()),
        )
    drafts = [
        "NCPL is up 40 percent.",
        "I have not looked at that one yet.",
    ]

    def completion(body):
        room.writes.append(body)
        return {"choices": [{"message": {"content": drafts.pop(0)}}]}

    monkeypatch.setattr(web_main, "_telegram_chat_completion", completion)
    _decide_with(monkeypatch, room, turn_payload())

    _run("anything moving")

    assert len(room.writes) == 2
    assert "NCPL" in json.loads(room.writes[1]["messages"][1]["content"])["instruction"]
    assert room.replies == ["I have not looked at that one yet."]


def test_an_unlooked_address_gets_one_rewrite_but_a_loaded_one_does_not(room, monkeypatch):
    from runner_web import main as web_main

    monkeypatch.setattr(web_main, "dash_expand", lambda node: {"known": False})
    monkeypatch.setattr(web_main, "memecoin_market", lambda **_: {"rows": []})

    def completion(body):
        room.writes.append(body)
        return {"choices": [{"message": {"content": f"{CA} is quiet."}}]}

    monkeypatch.setattr(web_main, "_telegram_chat_completion", completion)
    _decide_with(monkeypatch, room, turn_payload(needs=("coin", 0.9)))
    _run(f"what about {CA}", update_id=1)
    assert len(room.writes) == 1

    room.writes.clear()
    _decide_with(monkeypatch, room, turn_payload())
    _run("anything new", update_id=2)
    assert len(room.writes) == 2


def test_a_decided_comment_runs_once_across_a_retried_turn(room, monkeypatch):
    from runner_web import main as web_main

    with connection() as database:
        database.execute(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) "
            "VALUES(?,?,?,'NASDAQ',?)",
            (1, "MSGM", "Motorsport Games", NOW.isoformat()),
        )
    posted: list[tuple[str, str]] = []

    def comment(ticker, body, at=None):
        posted.append((ticker, body))
        return {"ok": True, "comment_id": f"c{len(posted)}", "body": body}

    monkeypatch.setattr(web_main, "dash_comment", comment)
    monkeypatch.setattr(
        web_main,
        "telegram_prefetch_for",
        lambda message, database: {
            "resolved_tickers": ["MSGM"],
            "looked_up": [{"ticker": "MSGM", "known": True}],
        },
    )
    _decide_with(monkeypatch, room, turn_payload(intent=("comment", 0.95)))
    message = chat.parse_update(_update("comment on $MSGM"), bot_username=BOT, bot_id=BOT_ID)

    web_main._decide_telegram_turn(message, [])
    writes_first = len(room.writes)
    web_main._decide_telegram_turn(message, [])

    assert [ticker for ticker, _ in posted] == ["MSGM"]
    # First turn: comment body + reply. Retry: the stored comment, so reply only.
    assert writes_first == 2
    assert len(room.writes) == 3
    context = json.loads(room.writes[-1]["messages"][1]["content"])
    assert context["your_action"]["result"]["already_done"] is True


def test_a_decided_call_runs_once_and_respects_the_budget(room, monkeypatch):
    from runner_web import main as web_main

    made: list[str] = []
    monkeypatch.setattr(
        web_main, "dash_make_call", lambda ticker: made.append(ticker) or {"ok": True}
    )
    monkeypatch.setattr(
        web_main,
        "telegram_prefetch_for",
        lambda message, database: {"resolved_tickers": ["MSGM"], "looked_up": []},
    )
    _decide_with(monkeypatch, room, turn_payload(intent=("open_call", 0.95)))
    message = chat.parse_update(_update("$MSGM call it"), bot_username=BOT, bot_id=BOT_ID)

    web_main._decide_telegram_turn(message, [])
    web_main._decide_telegram_turn(message, [])
    assert made == ["MSGM"]

    monkeypatch.setattr(web_main, "dash_budget", lambda at=None: dict(NO_BUDGET))
    other = chat.parse_update(
        _update("$MSGM call it", update_id=9), bot_username=BOT, bot_id=BOT_ID
    )
    web_main._decide_telegram_turn(other, [])
    assert made == ["MSGM"]


def test_the_turn_cap_still_holds_on_the_decision_path(room, monkeypatch):
    monkeypatch.setattr(chat, "TURNS_PER_HOUR", 1)
    _decide_with(monkeypatch, room, turn_payload(action=("hold", 0.9)))
    _run(update_id=1)
    result = _run(update_id=2)

    assert result["skipped"] == 1
    assert len(room.states) == 1


def test_the_decision_path_is_off_by_default():
    from runner_web import main as web_main

    assert web_main.DASH_DECISIONS_ENABLED is False
    assert web_main.DASH_DECISION_PROVIDER == {"zdr": True}


# ---------------------------------------------------------------------------
# The desk note
# ---------------------------------------------------------------------------


def _desk_payload(position: float, sure: float) -> dict[str, Any]:
    return {
        "answers": {
            "worth_speaking": {
                "type": "score",
                "score": position,
                "confidence": sure,
                "probabilities": {"0": 0, "1": 0, "2": 1, "3": 0},
            }
        },
        "usage": {"cost": 0.00005},
    }


@pytest.fixture
def desk(monkeypatch):
    from runner_web import main as web_main

    monkeypatch.setattr(web_main, "DASH_DESK_NOTES_ENABLED", True)
    monkeypatch.setattr(web_main, "DASH_DECISIONS_ENABLED", True)
    monkeypatch.setattr(web_main, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_API_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setattr(web_main, "telegram_room_chat_id", lambda: 123)
    sent: list[str] = []
    notes: list[Any] = []
    monkeypatch.setattr(
        web_main, "send_telegram_reply", lambda config, chat_id, text, **_: sent.append(text)
    )
    monkeypatch.setattr(
        web_main, "dash_world", lambda *a, **k: {"changes": {"any": True, "new_runners": 1}}
    )
    monkeypatch.setattr(
        web_main, "_generate_desk_note", lambda world: notes.append(world) or "Something moved."
    )
    return web_main, sent, notes


def test_a_low_worth_score_skips_the_desk_note_writer(desk, monkeypatch):
    web_main, sent, notes = desk
    monkeypatch.setattr(
        web_main,
        "_dash_decide",
        lambda state, questions: dec.parse_response(_desk_payload(0.8, 0.9), questions),
    )

    result = web_main.post_dash_desk_note(at=NOW)

    assert result == {"status": "held", "detail": "not_worth_speaking"}
    assert notes == [] and sent == []
    assert _logged()[0]["kind"] == "desk_note"
    assert _logged()[0]["action"] == "hold"


def test_a_high_worth_score_writes_the_desk_note(desk, monkeypatch):
    web_main, sent, notes = desk
    states: list[Any] = []

    def decide(state, questions):
        states.append(state)
        return dec.parse_response(_desk_payload(2.6, 0.9), questions)

    monkeypatch.setattr(web_main, "_dash_decide", decide)

    assert web_main.post_dash_desk_note(at=NOW)["status"] == "sent"
    assert sent == ["Something moved."]
    assert states[0]["changes"]["new_runners"] == 1


def test_a_failed_desk_decision_keeps_the_old_behaviour(desk, monkeypatch):
    web_main, sent, notes = desk

    def decide(state, questions):
        raise dec.DecisionError("http", "credits", status=402)

    monkeypatch.setattr(web_main, "_dash_decide", decide)

    assert web_main.post_dash_desk_note(at=NOW)["status"] == "sent"
    assert _logged()[0]["source"] == "fallback"


def test_the_desk_gate_is_not_asked_when_nothing_changed_or_too_soon(desk, monkeypatch):
    web_main, sent, notes = desk
    asked: list[Any] = []
    monkeypatch.setattr(web_main, "_dash_decide", lambda *a: asked.append(a))
    monkeypatch.setattr(web_main, "dash_world", lambda *a, **k: {"changes": {"any": False}})

    assert web_main.post_dash_desk_note(at=NOW)["status"] == "quiet"

    web_main.worker_state("dash_desk_note_last_at", NOW.isoformat())
    assert web_main.post_dash_desk_note(at=NOW + timedelta(minutes=5))["status"] == "waiting"
    assert asked == []
