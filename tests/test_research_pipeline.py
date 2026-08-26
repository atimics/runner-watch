from __future__ import annotations

from typing import Any

from runner_web.research_pipeline import run_verified_pipeline


def _context(*, hard_veto: bool = False) -> dict[str, Any]:
    return {
        "ticker": "ONE",
        "primary_evidence": {
            "captured_at": "2026-08-25T16:00:00+00:00",
            "trade_state": "AVOID" if hard_veto else "WATCH",
            "hard_veto": hard_veto,
            "state_reason": "Active offering risk" if hard_veto else "Needs proof",
        },
        "context_sections": [
            {
                "kind": "sec_filing",
                "observed_at": "2026-08-25T15:00:00+00:00",
                "source_url": "https://www.sec.gov/Archives/edgar/data/1/filing.txt",
                "data": {"form": "S-3", "summary": "Registration statement"},
            }
        ],
        "sources": [
            "https://www.sec.gov/Archives/edgar/data/1/filing.txt",
            "https://example.test/approved",
        ],
    }


def test_verified_pipeline_runs_five_roles_and_drops_unsupported_evidence() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_stage(
        stage: str,
        order: int,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del instructions, schema
        calls.append((stage, payload))
        metadata = {"provider": "test", "model": "test-model", "usage": {"total": order}}
        if order <= 3:
            return (
                {
                    "findings": [
                        {
                            "statement": f"Verified finding from {stage}",
                            "effect": "risks" if order == 2 else "supports",
                            "evidence_refs": ["E1"],
                            "source_urls": [
                                "https://www.sec.gov/Archives/edgar/data/1/filing.txt"
                            ],
                        },
                        {
                            "statement": "Unsupported rumor",
                            "effect": "supports",
                            "evidence_refs": ["MISSING"],
                            "source_urls": ["https://rumor.invalid/story"],
                        },
                    ],
                    "unknowns": ["Cash runway is unknown"],
                },
                metadata,
            )
        if stage == "independent_critic":
            review_statements = [
                finding["statement"]
                for review in payload["reviews"].values()
                for finding in review["findings"]
            ]
            assert "Unsupported rumor" not in review_statements
            return (
                {
                    "supported_statements": review_statements + ["Invented critic claim"],
                    "rejected_statements": [],
                    "conflicts": [],
                    "required_caveats": ["Financing terms remain unknown"],
                    "verdict": "mixed",
                },
                metadata,
            )
        assert "Invented critic claim" not in payload["critic"]["supported_statements"]
        return (
            {
                "headline": "ONE needs financing proof",
                "thesis": "The filing is verified, but its effect is mixed.",
                "summary": "Verified catalysts and financing risk point in different directions.",
                "case_effect": "mixed",
                "market_view": "neutral",
                "confidence": 0.58,
                "catalysts": ["Verified filing"],
                "risks": ["Possible dilution"],
                "watch": ["Final financing terms"],
                "unknowns": ["Cash runway"],
                "sources": [
                    "https://www.sec.gov/Archives/edgar/data/1/filing.txt",
                    "https://rumor.invalid/story",
                ],
            },
            metadata,
        )

    report, trace = run_verified_pipeline(_context(), fake_stage)

    assert [stage for stage, _ in calls] == [
        "catalyst_researcher",
        "financing_skeptic",
        "market_liquidity_checker",
        "independent_critic",
        "synthesis",
    ]
    assert [item["stage_order"] for item in trace] == [1, 2, 3, 4, 5]
    assert report["sources"] == [
        "https://www.sec.gov/Archives/edgar/data/1/filing.txt"
    ]
    assert "Invented critic claim" not in report["critic"]["supported_statements"]
    assert "Verified finding from catalyst_researcher" in report["catalysts"]
    assert "Verified finding from financing_skeptic" in report["risks"]
    assert "Possible dilution" not in report["risks"]
    assert report["deterministic_override"] is False


def test_deterministic_veto_overrides_the_model_report() -> None:
    def fake_stage(
        stage: str,
        order: int,
        instructions: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        del instructions, payload, schema
        metadata = {"provider": "test", "model": "test-model"}
        if order <= 3:
            return {"findings": [], "unknowns": []}, metadata
        if stage == "independent_critic":
            return (
                {
                    "supported_statements": [],
                    "rejected_statements": [],
                    "conflicts": [],
                    "required_caveats": [],
                    "verdict": "unchanged",
                },
                metadata,
            )
        return (
            {
                "headline": "Strong bullish setup",
                "thesis": "Buy now.",
                "summary": "Momentum is strong.",
                "case_effect": "strengthened",
                "market_view": "bullish",
                "confidence": 0.95,
                "catalysts": [],
                "risks": [],
                "watch": [],
                "unknowns": [],
                "sources": [],
            },
            metadata,
        )

    report, _ = run_verified_pipeline(_context(hard_veto=True), fake_stage)

    assert report["headline"] == "Risk veto active — ONE"
    assert report["thesis"] == "Thesis weakened. Active offering risk"
    assert report["risks"][0] == "Active offering risk"
    assert report["deterministic_override"] is True
