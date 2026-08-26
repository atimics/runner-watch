from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.cases import create_case
from runner_web.db import connection, init_db
from runner_web.research_policy import research_policy_scorecards


def test_research_policies_are_ranked_by_linked_case_outcomes(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "research-policy.db")
    init_db()
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("policy-user", "policy_user", "Policy User", "active", timestamp),
        )
    first = create_case(
        "policy-user",
        "ONE",
        thesis="ONE can move higher.",
        horizon_minutes=60,
        reference_price=1.0,
        invalidation="Unknown",
        risks=[],
        open_questions=[],
        confidence=None,
    )
    second = create_case(
        "policy-user",
        "TWO",
        thesis="TWO can move higher.",
        horizon_minutes=60,
        reference_price=1.0,
        invalidation="Unknown",
        risks=[],
        open_questions=[],
        confidence=None,
    )
    with connection() as database:
        for case, ticker, result in ((first, "ONE", 10.0), (second, "TWO", 5.0)):
            database.execute(
                """
                INSERT INTO thesis_case_outcomes(
                    case_id,ticker,base_price,base_at,horizon_minutes,due_at,status,
                    end_price,observed_at,return_pct,return_direction,
                    max_favorable_pct,max_adverse_pct,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'complete',?,?,?,?,?,?,?,?)
                """,
                (
                    case["id"],
                    ticker,
                    1.0,
                    timestamp,
                    60,
                    timestamp,
                    1.0 + result / 100,
                    timestamp,
                    result,
                    "up",
                    result,
                    0.0,
                    timestamp,
                    timestamp,
                ),
            )
        for report_id, case, ticker, model, view, confidence in (
            ("report-terra", first, "ONE", "gpt-5.6-terra", "bullish", 0.8),
            ("report-terra-repeat", first, "ONE", "gpt-5.6-terra", "bullish", 0.9),
            ("report-luna", second, "TWO", "gpt-5.6-luna", "bearish", 0.7),
        ):
            database.execute(
                """
                INSERT INTO research_commissions(
                    id,public_id,user_id,ticker,evidence_key,status,requested_model,
                    model,research_mode,case_id,market_view,model_confidence,
                    policy_version,created_at,updated_at,completed_at
                ) VALUES(?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    report_id,
                    f"public-{report_id}",
                    "policy-user",
                    ticker,
                    f"evidence-{ticker}",
                    model,
                    model,
                    "verified_agent_pipeline",
                    case["id"],
                    view,
                    confidence,
                    "verified-research-v1",
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )

    scorecards = research_policy_scorecards()

    assert [row["model"] for row in scorecards] == [
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    ]
    assert scorecards[0]["accuracy"] == 1.0
    assert scorecards[0]["brier_score"] == 0.04
    assert scorecards[0]["reports"] == 2
    assert scorecards[0]["outcomes"] == 1
    assert scorecards[0]["brier_skill_score"] == 84.0
    assert scorecards[1]["accuracy"] == 0.0
    assert scorecards[1]["brier_score"] == 0.49
    assert scorecards[1]["brier_skill_score"] == -96.0
    assert all(row["state"] == "learning" for row in scorecards)
    assert all(not row["eligible_for_promotion"] for row in scorecards)


def test_research_policy_promotion_needs_independent_diverse_cases(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "eligible-policy.db")
    init_db()
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("eligible-user", "eligible_user", "Eligible User", "active", timestamp),
        )
    for index in range(50):
        ticker = f"P{index:02d}"
        case = create_case(
            "eligible-user",
            ticker,
            thesis=f"{ticker} can move higher.",
            horizon_minutes=60,
            reference_price=1.0,
            invalidation="Unknown",
            risks=[],
            open_questions=[],
            confidence=None,
        )
        with connection() as database:
            database.execute(
                """
                INSERT INTO thesis_case_outcomes(
                    case_id,ticker,base_price,base_at,horizon_minutes,due_at,status,
                    end_price,observed_at,return_pct,return_direction,
                    max_favorable_pct,max_adverse_pct,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'complete',?,?,?,?,?,?,?,?)
                """,
                (
                    case["id"],
                    ticker,
                    1.0,
                    timestamp,
                    60,
                    timestamp,
                    1.1,
                    timestamp,
                    10.0,
                    "up",
                    10.0,
                    0.0,
                    timestamp,
                    timestamp,
                ),
            )
            database.execute(
                """
                INSERT INTO research_commissions(
                    id,public_id,user_id,ticker,evidence_key,status,requested_model,
                    model,research_mode,case_id,market_view,model_confidence,
                    policy_version,created_at,updated_at,completed_at
                ) VALUES(?,?,?,?,?,'complete',?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"report-{index}",
                    f"public-report-{index}",
                    "eligible-user",
                    ticker,
                    f"evidence-{index}",
                    "gpt-5.6-terra",
                    "gpt-5.6-terra",
                    "verified_agent_pipeline",
                    case["id"],
                    "bullish",
                    0.8,
                    "verified-research-v1",
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )

    scorecard = research_policy_scorecards()[0]

    assert scorecard["outcomes"] == 50
    assert scorecard["tickers"] == 50
    assert scorecard["accuracy_lower_bound"] > 0.5
    assert scorecard["eligible_for_promotion"] is True
    assert scorecard["state"] == "eligible"
