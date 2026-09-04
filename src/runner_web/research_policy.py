from __future__ import annotations

from typing import Any

from runner_web.db import connection
from runner_web.product_policy import RESEARCH_PROMOTION

MIN_POLICY_OUTCOMES = RESEARCH_PROMOTION.learning_cases


def _wilson_lower_bound(successes: int, total: int) -> float | None:


    if total <= 0:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1.0 + z**2 / total
    center = proportion + z**2 / (2 * total)
    margin = z * (
        (proportion * (1.0 - proportion) + z**2 / (4 * total)) / total
    ) ** 0.5
    return max(0.0, (center - margin) / denominator)


def _probability_up(view: str, confidence: float) -> float:
    bounded = max(0.0, min(1.0, confidence))
    if view == "bullish":
        return bounded
    if view == "bearish":
        return 1.0 - bounded
    return 0.5


def research_policy_scorecards() -> list[dict[str, Any]]:


    with connection() as db:
        rows = [
            dict(row)
            for row in db.execute(
                """
                SELECT r.id,r.case_id,r.ticker,r.completed_at,r.policy_version,r.model,
                       r.market_view,r.model_confidence,r.status AS report_status,
                       o.status AS outcome_status,o.return_pct
                FROM research_commissions r
                LEFT JOIN thesis_case_outcomes o ON o.case_id=r.case_id
                WHERE r.research_mode='verified_agent_pipeline'
                      AND r.policy_version IS NOT NULL AND r.model IS NOT NULL
                ORDER BY r.completed_at,r.id
                """
            ).fetchall()
        ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (str(row["policy_version"]), str(row["model"]))
        grouped.setdefault(key, []).append(row)
    scorecards: list[dict[str, Any]] = []
    for (policy, model), reports in grouped.items():
        resolved_by_case: dict[str, dict[str, Any]] = {}
        for row in reports:
            case_id = str(row.get("case_id") or "")
            if (
                not case_id
                or row.get("report_status") != "complete"
                or row.get("outcome_status") != "complete"
                or row.get("return_pct") is None
                or row.get("market_view") not in {"bullish", "bearish", "neutral"}
                or row.get("model_confidence") is None
            ):
                continue
            resolved_by_case.setdefault(case_id, row)
        resolved = list(resolved_by_case.values())
        directional = [row for row in resolved if abs(float(row["return_pct"])) > 0.5]
        squared_errors: list[float] = []
        correct = 0
        for row in directional:
            actual_up = float(row["return_pct"]) > 0.5
            predicted_up = _probability_up(
                str(row["market_view"]),
                float(row["model_confidence"]),
            )
            squared_errors.append((predicted_up - float(actual_up)) ** 2)
            correct += int(
                (row["market_view"] == "bullish" and actual_up)
                or (row["market_view"] == "bearish" and not actual_up)
            )
        brier = sum(squared_errors) / len(squared_errors) if squared_errors else None
        outcomes = len(directional)
        tickers = len({str(row["ticker"]) for row in directional})
        accuracy = correct / outcomes if outcomes else None
        accuracy_lower_bound = _wilson_lower_bound(correct, outcomes)
        brier_skill = 1.0 - brier / 0.25 if brier is not None else None
        promotion_checks = {
            "independent_cases": outcomes >= RESEARCH_PROMOTION.promotion_cases,
            "ticker_diversity": tickers >= RESEARCH_PROMOTION.promotion_tickers,
            "accuracy_lower_bound": bool(
                accuracy_lower_bound is not None
                and accuracy_lower_bound > RESEARCH_PROMOTION.minimum_accuracy_lower_bound
            ),
            "brier_score": bool(
                brier is not None and brier < RESEARCH_PROMOTION.maximum_brier_score
            ),
        }
        eligible = all(promotion_checks.values())
        scorecards.append(
            {
                "policy_version": policy,
                "model": model,
                "reports": len(reports),
                "pending_outcomes": len(
                    {
                        str(row.get("case_id"))
                        for row in reports
                        if row.get("case_id") and row.get("outcome_status") != "complete"
                    }
                ),
                "resolved_cases": len(resolved),
                "outcomes": outcomes,
                "tickers": tickers,
                "accuracy": round(accuracy, 4) if accuracy is not None else None,
                "accuracy_lower_bound": (
                    round(accuracy_lower_bound, 4)
                    if accuracy_lower_bound is not None
                    else None
                ),
                "brier_score": round(brier, 4) if brier is not None else None,
                "brier_skill_score": (
                    round(100.0 * brier_skill, 2)
                    if brier_skill is not None
                    else None
                ),
                "promotion_checks": promotion_checks,
                "eligible_for_promotion": eligible,
            }
        )
    scorecards.sort(
        key=lambda item: (
            not item["eligible_for_promotion"],
            -(item["brier_skill_score"] or 0.0),
            -(item["accuracy_lower_bound"] or 0.0),
            -item["outcomes"],
            item["policy_version"],
            item["model"],
        )
    )
    for rank, item in enumerate(scorecards, start=1):
        item["rank"] = rank
        if item["eligible_for_promotion"]:
            item["state"] = "eligible"
        elif item["outcomes"] >= RESEARCH_PROMOTION.learning_cases:
            item["state"] = "evaluating"
        else:
            item["state"] = "learning"
        item["promotion_requirements"] = {
            "independent_cases": RESEARCH_PROMOTION.promotion_cases,
            "distinct_tickers": RESEARCH_PROMOTION.promotion_tickers,
            "minimum_accuracy_lower_bound": (
                RESEARCH_PROMOTION.minimum_accuracy_lower_bound
            ),
            "maximum_brier_score": RESEARCH_PROMOTION.maximum_brier_score,
        }
    return scorecards
