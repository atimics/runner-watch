"""What Dash does with a message, decided by a decision model and plain rules.

A cheap decision model answers a few typed questions about a small state: should
Dash engage, did the person ask him to stop, reply or react or hold, what data
the reply needs, and whether he means to open or close a Call or comment. It
returns probabilities, not words.

The rules here turn those probabilities into one plan. They are pure functions,
so the thresholds can be tested and tuned without a model. The chat model only
writes words, and only when the plan is to reply.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from runner_web.decisions import Answer, choice, noul, score


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


# Below this, an answer is not trusted and Dash holds (or reacts, when addressed).
CONFIDENCE = _env_float("DASH_DECISION_CONFIDENCE", 0.6)
# Public Calls and paid comments need a higher bar.
MUTATION_CONFIDENCE = _env_float("DASH_DECISION_MUTATION_CONFIDENCE", 0.8)
# The desk note plays only when worth_speaking lands at or above this level.
DESK_NOTE_MIN_SCORE = _env_float("DASH_DESK_NOTE_MIN_SCORE", 2.0)

NEEDS_NODES = {
    "board": "board",
    "runners": "runners",
    "events": "events",
    "halts": "halts",
    "sports": "sports",
    "memecoins": "memecoins",
}

TURN_QUESTIONS: dict[str, dict[str, Any]] = {
    "should_engage": noul(
        "Dash is a cheetah bot in a trading chat room. Should Dash take part in "
        "this message at all? Look at who it is aimed at and the last few lines.",
        "The message is aimed at Dash, asks him something, answers something he "
        "said, or is a market question he can help with.",
        "People are talking to each other, the message is small talk not aimed at "
        "him, or anything he adds would only repeat what he already said.",
    ),
    "stop_requested": noul(
        "Is the speaker asking Dash to stop talking, go away, or leave them alone?",
        "The speaker tells Dash to stop, be quiet, shut up, leave them alone, or "
        "says they do not want his replies.",
        "The speaker does not ask Dash to stop. Disagreeing with him or joking is "
        "not asking him to stop.",
    ),
    "action": choice(
        "What should Dash do with this message?",
        {
            "reply": "Write a short answer. Use this when there is a question, a "
            "request, or something Dash can add with market facts.",
            "react": "Put one emoji on the message. Use this when the message "
            "deserves an acknowledgement but has nothing to answer.",
            "hold": "Do nothing. Use this when the message is not for Dash, when he "
            "would repeat himself, or when he was asked to stop.",
        },
    ),
    "needs": choice(
        "If Dash replies, which data does he need to load first to answer correctly?",
        {
            "none": "No market data; small talk, a greeting, or a question about Dash.",
            "board": "The stock board: what is moving, green or red, the leaders.",
            "runners": "Stocks that just entered the board as new runners.",
            "events": "Recent news, coverage, or social spikes.",
            "halts": "Trading halts today, why, and how stocks traded after reopening.",
            "sports": "Sports games, scores, and the model's pregame lean.",
            "memecoins": "The memecoin board: most traded coins, big moves, flags.",
            "ticker": "One named stock ticker, such as $MSGM or MSGM.",
            "coin": "One memecoin named by its contract address.",
        },
    ),
    "call_intent": choice(
        "Does the conversation clearly call for Dash to act on his own public "
        "record? Only choose an action when the message is about one named stock "
        "and acting is plainly the point. Being asked is not enough to open a Call.",
        {
            "none": "No action on the record. This is the normal answer.",
            "open_call": "Dash should open a public paper Call on the named stock.",
            "close_call": "Dash should close his own open Call on the named stock.",
            "comment": "Dash should leave a public comment on the named stock's page.",
        },
    ),
}

DESK_NOTE_QUESTIONS: dict[str, dict[str, Any]] = {
    "worth_speaking": score(
        "Nobody asked Dash anything. Given what changed in the market since his "
        "last note, how worth it is for him to post one short note in the room now?",
        [
            "Nothing worth a note: no real change, or the same as the last note.",
            "Minor: small changes most people would not care about.",
            "Worth a short note: a new runner, a halt, a reopen, or a new report.",
            "The room should hear this now: a big move or several things at once.",
        ],
    ),
}


def turn_state(
    *,
    speaker: str,
    text: str,
    addressed: bool,
    transcript: list[Mapping[str, Any]],
    recent_actions: list[Mapping[str, Any]],
    budget: Mapping[str, Any],
    changes: Mapping[str, Any],
    session: str,
    tickers: list[str],
    addresses: list[str],
) -> dict[str, Any]:
    """The small state one turn decision sees. Not the world."""

    return {
        "message": {"speaker": speaker, "text": text[:800], "addressed_to_dash": addressed},
        "recent_lines": [
            {"who": str(line.get("who") or ""), "said": str(line.get("said") or "")[:200]}
            for line in transcript[-4:]
        ],
        "dash_recent_actions": [
            {"action": row.get("action"), "detail": str(row.get("detail") or "")[:120]}
            for row in recent_actions[:3]
        ],
        "tickers_named": tickers,
        "contract_addresses_named": addresses,
        "market_session": session,
        "dash_budget": {
            "can_call": bool(budget.get("can_call")),
            "can_comment": bool(budget.get("can_comment")),
            "calls_left": int(budget.get("calls_left") or 0),
        },
        "changes": {
            key: changes.get(key)
            for key in (
                "new_runners",
                "events",
                "reopened_halts",
                "session_reports",
                "public_reports",
                "any",
            )
            if key in changes
        },
    }


def desk_note_state(world: Mapping[str, Any], *, last_note: str, minutes_since: int | None):
    """The small state the desk note decision sees."""

    board = world.get("board") or {}
    runners = world.get("runners") or {}
    return {
        "changes": world.get("changes") or {},
        "market_session": (world.get("session") or {}).get("label"),
        "board": {
            "green": board.get("green"),
            "red": board.get("red"),
            "average_change_pct": board.get("average_change_pct"),
            "leaders": [
                {"ticker": row.get("ticker"), "change_pct": row.get("change_pct")}
                for row in (board.get("leaders") or [])[:3]
            ],
        },
        "new_runners": [row.get("ticker") for row in (runners.get("entries") or [])[:5]],
        "events": [
            {"kind": row.get("kind"), "ticker": row.get("ticker")}
            for row in (world.get("events") or [])[:4]
        ],
        "halts_today": len(world.get("halts") or []),
        "last_note": last_note[:300],
        "minutes_since_last_note": minutes_since,
    }


@dataclass(frozen=True, slots=True)
class TurnPlan:
    """What code will do for one message."""

    action: str  # reply | react | hold
    reason: str
    mute: bool = False
    emoji: str = ""
    node: str | None = None
    mutation: str | None = None  # make_call | close_call | comment_on_ticker
    ticker: str | None = None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


MUTATIONS = {"open_call": "make_call", "close_call": "close_call", "comment": "comment_on_ticker"}


def _picked(answer: Answer) -> tuple[str, float]:
    """The chosen option and how sure the model is of it.

    The gate uses the probability of the chosen option; when the response has
    no probabilities it uses the reported confidence.
    """

    option = str(answer.value)
    sure = answer.probability(option) if answer.probabilities else float(answer.confidence or 0)
    return option, sure


def choose_turn(
    answers: Mapping[str, Answer],
    *,
    addressed: bool,
    budget: Mapping[str, Any],
    tickers: list[str],
    addresses: list[str],
    confidence: float | None = None,
    mutation_confidence: float | None = None,
) -> TurnPlan:
    """Turn the decision model's probabilities into one plan.

    - A clear stop request holds and mutes that person.
    - An action the model is not sure of becomes hold, or a react when the
      message was addressed to Dash, so a direct question is acknowledged.
    - A message not addressed to Dash also needs should_engage to pass.
    - Loading data and acting on the record only happen on a reply. Acting on
      the record needs the higher bar, a named ticker, and budget left.
    """

    bar = CONFIDENCE if confidence is None else confidence
    high_bar = MUTATION_CONFIDENCE if mutation_confidence is None else mutation_confidence

    if float(answers["stop_requested"].value) >= bar:
        return TurnPlan("hold", "stop_requested", mute=True)

    action, sure = _picked(answers["action"])
    if sure < bar:
        if addressed:
            return TurnPlan("react", "low_confidence", emoji="👀")
        return TurnPlan("hold", "low_confidence")
    if not addressed and float(answers["should_engage"].value) < bar:
        return TurnPlan("hold", "not_engaging")
    if action == "react":
        return TurnPlan("react", "decided", emoji="🐆")
    if action != "reply":
        return TurnPlan("hold", "decided")

    reasons = ["decided"]
    node: str | None = None
    need, need_sure = _picked(answers["needs"])
    if need_sure >= bar:
        if need in NEEDS_NODES:
            node = NEEDS_NODES[need]
        elif need == "ticker" and tickers:
            node = f"ticker:{tickers[0]}"
        elif need == "coin" and addresses:
            node = f"coin:{addresses[0]}"

    mutation: str | None = None
    ticker: str | None = None
    intent, intent_sure = _picked(answers["call_intent"])
    if intent in MUTATIONS and intent_sure >= high_bar:
        if not tickers:
            reasons.append("no_ticker_for_" + intent)
        elif intent == "open_call" and not (
            budget.get("can_call") and int(budget.get("calls_left") or 0) > 0
        ):
            reasons.append("no_calls_left")
        elif intent == "comment" and not budget.get("can_comment"):
            reasons.append("no_comments_left")
        else:
            mutation = MUTATIONS[intent]
            ticker = tickers[0]
    return TurnPlan("reply", ",".join(reasons), node=node, mutation=mutation, ticker=ticker)


def desk_note_worth_it(
    answer: Answer, *, min_score: float | None = None, confidence: float | None = None
) -> bool:
    """Speak only when the score clears the bar and the model is sure enough."""

    floor = DESK_NOTE_MIN_SCORE if min_score is None else min_score
    bar = CONFIDENCE if confidence is None else confidence
    return float(answer.value) >= floor and float(answer.confidence or 0) >= bar
