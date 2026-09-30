"""A small client for OpenRouter's Decisions API (alpha).

The Decisions API is not a text model. It takes a state and a set of typed
questions and returns probabilities:

- ``noul``: a yes/no question. The answer is the probability of yes.
- ``choice``: pick one option. The answer is the picked option, a confidence,
  and a probability for each option.
- ``score``: place the state on an ordered scale. The answer is a
  probability-weighted position, a confidence, and a probability per level.

The endpoint is alpha, so everything that knows its URL, its request body or
its response shape lives in this file. ``parse_response`` is a pure function,
so a schema change is one file and its tests.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

DECISIONS_URL = "https://openrouter.ai/api/v1/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
TIMEOUT_SECONDS = 15
QUESTION_TYPES = ("noul", "choice", "score")
MAX_RESPONSE_BYTES = 262_144


def decision_model() -> str:
    """The decision model id. DASH_DECISION_MODEL overrides the default."""

    return os.getenv("DASH_DECISION_MODEL", "").strip() or DEFAULT_MODEL


class DecisionError(Exception):
    """The decision call failed or its answer could not be read.

    ``status`` is the HTTP status when there was one (402 means out of credits).
    ``kind`` is a short word for logs: http, network, malformed.
    """

    def __init__(self, kind: str, detail: str = "", status: int | None = None) -> None:
        self.kind = kind
        self.detail = detail[:300]
        self.status = status
        label = f"{kind} {status}" if status is not None else kind
        super().__init__(f"{label}: {self.detail}" if self.detail else label)


@dataclass(frozen=True, slots=True)
class Answer:
    """One typed answer.

    ``value`` is a float for noul (probability of yes) and score (the weighted
    position), and the option name for choice. ``confidence`` is None for noul,
    which has none.
    """

    type: str
    value: Any
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)

    def probability(self, option: str) -> float:
        """How likely one option (or one score level) is. Missing means zero."""

        return float(self.probabilities.get(str(option), 0.0))

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "value": self.value}
        if self.confidence is not None:
            payload["confidence"] = self.confidence
        if self.probabilities:
            payload["probabilities"] = self.probabilities
        return payload


@dataclass(frozen=True, slots=True)
class Decision:
    """A parsed Decisions response."""

    answers: dict[str, Answer]
    id: str = ""
    model: str = ""
    provider: str = ""
    cost: float | None = None

    def answers_json(self) -> dict[str, Any]:
        return {key: answer.as_json() for key, answer in self.answers.items()}


def noul(instructions: str, true: str, false: str) -> dict[str, Any]:
    return {
        "type": "noul",
        "instructions": instructions,
        "criteria": {"true": true, "false": false},
    }


def choice(instructions: str, options: Mapping[str, str]) -> dict[str, Any]:
    return {"type": "choice", "instructions": instructions, "criteria": dict(options)}


def score(instructions: str, levels: list[str]) -> dict[str, Any]:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def build_request(
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    model: str | None = None,
    provider: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The request body. Only documented fields are sent."""

    for key, question in questions.items():
        if question.get("type") not in QUESTION_TYPES:
            raise ValueError(f"question {key!r} has an unknown type")
    body: dict[str, Any] = {
        "model": model or decision_model(),
        "state": state,
        "questions": {key: dict(question) for key, question in questions.items()},
    }
    if provider:
        body["provider"] = dict(provider)
    return body


def _unit(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecisionError("malformed", f"{what} is not a number")
    number = float(value)
    if not math.isfinite(number) or number < -1e-6 or number > 1 + 1e-6:
        raise DecisionError("malformed", f"{what} is outside 0..1")
    return min(1.0, max(0.0, number))


def _probabilities(raw: Any, allowed: set[str], what: str) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        raise DecisionError("malformed", f"{what} has no probabilities")
    parsed: dict[str, float] = {}
    for key, value in raw.items():
        name = str(key)
        if name not in allowed:
            raise DecisionError("malformed", f"{what} has unknown option {name!r}")
        parsed[name] = _unit(value, f"{what}.{name}")
    return parsed


def _parse_answer(key: str, raw: Any, question: Mapping[str, Any]) -> Answer:
    kind = question.get("type")
    if not isinstance(raw, Mapping):
        raise DecisionError("malformed", f"answer {key} is not an object")
    if raw.get("type") not in (None, kind):
        raise DecisionError("malformed", f"answer {key} has type {raw.get('type')!r}")
    if kind == "noul":
        return Answer("noul", _unit(raw.get("noul"), f"{key}.noul"))
    if kind == "choice":
        options = set(map(str, question.get("criteria") or {}))
        picked = raw.get("choice")
        if not isinstance(picked, str) or picked not in options:
            raise DecisionError("malformed", f"answer {key} picked an unknown option")
        return Answer(
            "choice",
            picked,
            _unit(raw.get("confidence"), f"{key}.confidence"),
            _probabilities(raw.get("probabilities"), options, key),
        )
    levels = {str(index) for index in range(len(question.get("criteria") or []))}
    position = raw.get("score")
    if isinstance(position, bool) or not isinstance(position, (int, float)):
        raise DecisionError("malformed", f"answer {key} has no score")
    if not math.isfinite(float(position)) or not 0 <= float(position) <= len(levels) - 1 + 1e-6:
        raise DecisionError("malformed", f"answer {key} score is off the scale")
    return Answer(
        "score",
        float(position),
        _unit(raw.get("confidence"), f"{key}.confidence"),
        _probabilities(raw.get("probabilities"), levels, key),
    )


def parse_response(payload: Any, questions: Mapping[str, Mapping[str, Any]]) -> Decision:
    """Read a Decisions response against the questions that were asked.

    Every question must come back with an answer of the type that was asked,
    a choice must be one of the offered options, and every probability must be
    in 0..1. Anything else raises DecisionError("malformed"), so a caller never
    acts on half an answer.
    """

    if not isinstance(payload, Mapping):
        raise DecisionError("malformed", "response is not an object")
    raw_answers = payload.get("answers")
    if not isinstance(raw_answers, Mapping):
        raise DecisionError("malformed", "response has no answers")
    answers = {}
    for key, question in questions.items():
        if key not in raw_answers:
            raise DecisionError("malformed", f"answer {key} is missing")
        answers[key] = _parse_answer(key, raw_answers[key], question)
    usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
    cost = usage.get("cost")
    return Decision(
        answers=answers,
        id=str(payload.get("id") or ""),
        model=str(payload.get("model") or ""),
        provider=str(payload.get("provider") or ""),
        cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
    )


def _post(body: dict[str, Any], *, api_key: str, referer: str, title: str) -> Any:
    request = urllib.request.Request(
        DECISIONS_URL,
        data=json.dumps(body, separators=(",", ":"), default=str).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": referer,
            "X-OpenRouter-Title": title,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - the body is only for the log
            detail = ""
        raise DecisionError("http", detail, status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DecisionError("network", type(exc).__name__) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise DecisionError("malformed", "response too large")
    try:
        return json.loads(raw)
    except ValueError:
        raise DecisionError("malformed", "response is not JSON") from None


def decide(
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
    *,
    api_key: str,
    referer: str = "",
    title: str = "RATi Runners decisions",
    provider: Mapping[str, Any] | None = None,
    model: str | None = None,
    post: Callable[..., Any] | None = None,
) -> Decision:
    """Ask the questions about the state and return the parsed answers.

    Raises DecisionError on any failure: no key, HTTP error (402 is out of
    credits, 429 is rate limited), timeout, or an answer that does not parse.
    """

    if not api_key:
        raise DecisionError("http", "no API key", status=401)
    body = build_request(state, questions, model=model, provider=provider)
    payload = (post or _post)(body, api_key=api_key, referer=referer, title=title)
    return parse_response(payload, questions)
