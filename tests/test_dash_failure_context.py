from __future__ import annotations

import json
from datetime import timedelta

import pytest

from runner_web import dash_jobs, dash_research, dash_sources
from runner_web.db import connection
from tests.test_dash_research import NOW, SCOPE, fresh_db, run, turn  # noqa: F401

EMPTY = {"choices": [{"message": {"content": ""}}]}


def fail(*args, **kwargs):
    raise TimeoutError("https://private.invalid?api_key=secret-value")


def test_failed_turn_names_the_lookup_and_keeps_completed_checks():
    def expand(node):
        return fail() if node == "halts" else {"count": 2}

    result, requests = run(
        [turn(("expand", {"node": "board"}), ("expand", {"node": "halts"})), EMPTY, EMPTY],
        expand=expand,
    )
    status = result["research"]
    assert status["status"] == "incomplete"
    assert status["stop_reason"] == "empty_response"
    assert status["completed_nodes"] == ["board"]
    failure = status["failures"][0]
    assert failure == {
        "code": "lookup_failed",
        "stage": "lookup",
        "tool": "expand",
        "node": "halts",
        "retryable": True,
        "retry": {"tool": "expand", "arguments": {"node": "halts"}},
    }
    assert "expand for halts" in result["text"]
    assert "Completed checks: board" in result["text"]
    output = json.loads(requests[1]["messages"][4]["content"])
    assert output["failure"] == failure
    assert "secret-value" not in json.dumps([result, requests])


@pytest.mark.parametrize("initial", [{"known": False}, {"error": "Source unavailable"}, None])
def test_failed_expand_can_retry_while_successful_reads_stay_cached(initial):
    reads = []

    def expand(node):
        reads.append(node)
        if node == "halts" and reads.count("halts") == 1:
            return initial if initial is not None else fail()
        return {"count": 2}

    result, _ = run(
        [
            turn(("expand", {"node": "board"}), ("expand", {"node": "halts"})),
            turn(("expand", {"node": "board"}), ("expand", {"node": "halts"})),
            turn(("reply", {"text": "Both checks completed."})),
        ],
        expand=expand,
    )
    assert reads == ["board", "halts", "halts"]
    assert result == {"action": "reply", "text": "Both checks completed."}


def test_partial_reply_retains_failed_source_metadata(monkeypatch):
    identity = "a" * 24
    monkeypatch.setattr(dash_sources, "read_source", fail)
    result, requests = run(
        [
            turn(("read_source", {"evidence_id": identity, "question": "private question"})),
            turn(("reply", {"text": "The source check is still pending."})),
        ],
        evidence={"sources:TEST": {"sources": [{"evidence_id": identity}]}},
    )
    assert result["research"]["status"] == "partial"
    failure = result["research"]["failures"][0]
    assert failure["tool"] == "read_source" and failure["node"] == "source:" + identity
    assert failure["retry"] == {
        "tool": "read_source",
        "arguments": {"evidence_id": identity},
        "required_arguments": ["question"],
    }
    assert "private question" not in json.dumps(result)
    assert "secret-value" not in json.dumps([result, requests])


def test_source_listing_failure_identifies_its_ticker(monkeypatch):
    monkeypatch.setattr(dash_sources, "list_sources", fail)
    result, _ = run([turn(("list_sources", {"ticker": "$test"})), EMPTY, EMPTY])
    failure = result["research"]["failures"][0]
    assert failure["node"] == "ticker:TEST"
    assert failure["retry"] == {"tool": "list_sources", "arguments": {"ticker": "TEST"}}


def test_job_retry_keeps_failure_target_and_reuses_partial_evidence():
    result, _ = run(
        [turn(("expand", {"node": "board"}), ("expand", {"node": "halts"})), EMPTY, EMPTY],
        expand=lambda node: fail() if node == "halts" else {"count": 2},
        job_key="failed-check",
    )
    assert result["action"] == "retry"
    assert result["research"]["failures"][0]["node"] == "halts"
    reads = []
    result, requests = run(
        [
            turn(("expand", {"node": "board"}), ("expand", {"node": "halts"})),
            turn(("reply", {"text": "Both checks completed."})),
        ],
        expand=lambda node: reads.append(node) or {"count": 2},
        job_key="failed-check",
        at_override=NOW + timedelta(minutes=1),
    )
    assert reads == ["halts"]
    assert result == {"action": "reply", "text": "Both checks completed."}
    context = json.loads(requests[0]["messages"][1]["content"])
    assert context["failed_checks"][0]["node"] == "halts"


def test_exhausted_model_failure_is_saved_as_a_sanitized_result(monkeypatch):
    monkeypatch.setattr(dash_jobs, "MAX_ATTEMPTS", 1)
    result, _ = run([turn(("expand", {"node": "board"}))], job_key="model-failure")
    failure = result["research"]["failures"][0]
    assert failure["code"] == "model_failed"
    assert failure["stage"] == "model" and failure["tool"] == "complete"
    assert failure["retryable"] is True
    assert result["research"]["completed_nodes"] == ["board"]
    assert dash_jobs.completed_result("model-failure", SCOPE) == result


def test_public_action_failure_finishes_without_automatic_replay():
    calls = []

    def act(name, args):
        calls.append(name)
        fail()

    result, _ = run(
        [turn(("comment_on_ticker", {"ticker": "TEST", "body": "private body"}))],
        evidence={"ticker:TEST": {"price": 2}},
        act=act,
        job_key="action-failure",
    )
    failure = result["research"]["failures"][0]
    assert result["action"] == "reply"
    assert failure["stage"] == "action" and failure["tool"] == "comment_on_ticker"
    assert failure["node"] == "ticker:TEST"
    assert failure["retryable"] is False and failure["retry"] is None
    assert "Check its outcome before retrying" in result["text"]
    assert "private body" not in json.dumps(result)
    assert "secret-value" not in json.dumps(result)
    again, requests = run([], job_key="action-failure", act=act)
    assert again == result and requests == []
    assert calls == ["comment_on_ticker"]


def test_reply_validation_names_the_missing_evidence():
    result, _ = run(
        [
            turn(("reply", {"text": "Unsupported."})),
            turn(("reply", {"text": "Still unsupported."})),
        ],
        unbacked=lambda text, evidence: ["TEST"],
    )
    assert result["research"]["stop_reason"] == "unsupported_reply"
    assert result["research"]["failures"][0]["retry"] == {
        "tool": "expand",
        "arguments": {"node": "ticker:TEST"},
    }


def test_fresh_checkpoint_keeps_the_request_for_a_later_retry():
    request = {"room": {"said": "Check halt resolution for TEST."}}
    result, requests = run([EMPTY, EMPTY], context=request, job_key="original-request")
    assert result["action"] == "retry"
    assert json.loads(requests[0]["messages"][1]["content"])["room"] == request["room"]
    with connection() as database:
        saved = database.execute(
            "SELECT state_json FROM dash_research_jobs WHERE id='original-request'"
        ).fetchone()
    assert json.loads(saved["state_json"])["request"] == request
    _, requests = run(
        [turn(("reply", {"text": "The check is complete."}))],
        job_key="original-request",
        context={"room": {"said": "New context."}},
        at_override=NOW + timedelta(minutes=1),
    )
    assert json.loads(requests[0]["messages"][1]["content"])["original_request"] == request


def test_retry_receives_a_fresh_budget_without_a_stale_failure(monkeypatch):
    monkeypatch.setattr(dash_research, "MAX_ROUNDS", 2)
    result, _ = run(
        [turn(("expand", {"node": "board"})), turn(("expand", {"node": "halts"}))],
        job_key="budget-retry",
    )
    assert result["research"]["stop_reason"] == "round_limit"
    result, _ = run(
        [turn(("reply", {"text": "The check is complete."}))],
        job_key="budget-retry",
        at_override=NOW + timedelta(minutes=1),
    )
    assert result == {"action": "reply", "text": "The check is complete."}


def test_action_failure_is_not_replayed_after_result_persistence_fails(monkeypatch):
    finish = dash_jobs.finish

    def fail_finish(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(dash_jobs, "finish", fail_finish)
    with pytest.raises(RuntimeError, match="database unavailable"):
        run(
            [turn(("make_call", {"ticker": "TEST"}))],
            evidence={"ticker:TEST": {"price": 2}},
            act=fail,
            job_key="action-save-failure",
        )
    monkeypatch.setattr(dash_jobs, "finish", finish)
    result, requests = run(
        [],
        act=lambda *args: pytest.fail("An uncertain action must not run again"),
        job_key="action-save-failure",
        at_override=NOW + timedelta(minutes=1),
    )
    assert requests == []
    assert result["research"]["failures"][0]["retryable"] is False
    assert dash_jobs.completed_result("action-save-failure", SCOPE) == result


def test_standing_is_a_retryable_read_and_other_checks_can_continue():
    calls = []
    reads = []

    def act(name, args):
        calls.append(name)
        return fail() if len(calls) == 1 else {"budget": {}, "open_calls": []}

    result, requests = run(
        [
            turn(("my_standing", {}), ("expand", {"node": "board"})),
            turn(("my_standing", {})),
            turn(("reply", {"text": "The standing check recovered."})),
        ],
        act=act,
        expand=lambda node: reads.append(node) or {"count": 2},
        job_key="standing-retry",
    )
    first_failure = json.loads(requests[1]["messages"][3]["content"])["failure"]
    assert first_failure["code"] == "lookup_failed"
    assert first_failure["stage"] == "lookup" and first_failure["retryable"] is True
    assert first_failure["retry"] == {"tool": "my_standing", "arguments": {}}
    assert calls == ["my_standing", "my_standing"] and reads == ["board"]
    assert result == {"action": "reply", "text": "The standing check recovered."}
