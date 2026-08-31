from dataclasses import replace
from pathlib import Path

from runner_watch.source_catalog import DEFAULT_SOURCE_POLICIES
from runner_web.product_policy import (
    PRODUCT_POLICY_VERSION,
    RANKER_TRAINING,
    policy_manifest,
)

ROOT = Path(__file__).parents[1]


def test_policy_manifest_is_machine_readable_and_reports_source_review_drift() -> None:
    trade_halts = next(
        policy
        for policy in DEFAULT_SOURCE_POLICIES
        if policy.source == "nasdaq_trader" and policy.feed == "trade_halts"
    )
    policies = [
        replace(
            trade_halts,
            enabled=True,
            review_status="poc_only",
            display_policy="source_link_with_attribution",
        )
    ]

    manifest = policy_manifest(policies, product="runners")

    assert manifest["version"] == PRODUCT_POLICY_VERSION
    assert manifest["product"] == "runners"
    assert manifest["ranker_training"] == {
        "minimum_groups": 160,
        "minimum_rows": 5_000,
        "minimum_per_outcome": 20,
        "validation_fraction": 0.10,
        "test_fraction": 0.10,
        "maximum_groups": 320,
        "minimum_new_groups": 16,
        "interval_seconds": 21_600,
    }
    assert manifest["source_policy_warnings"] == [
        {
            "source": "nasdaq_trader",
            "feed": "trade_halts",
            "review_status": "poc_only",
            "severity": "blocking",
            "warning": "enabled source has not been approved for public product effects",
        }
    ]
    assert RANKER_TRAINING.minimum_rows == 5_000


def test_policy_manifest_ignores_internal_and_other_product_sources() -> None:
    trade_halts = next(
        policy
        for policy in DEFAULT_SOURCE_POLICIES
        if policy.source == "nasdaq_trader" and policy.feed == "trade_halts"
    )
    sports_preview = next(
        policy
        for policy in DEFAULT_SOURCE_POLICIES
        if policy.source == "espn" and policy.feed == "sports_scoreboard_preview"
    )

    manifest = policy_manifest(
        [replace(trade_halts, enabled=True), replace(sports_preview, enabled=True)],
        product="runners",
    )

    assert manifest["source_policy_warnings"] == []


def test_sports_does_not_accept_or_publish_human_written_comments() -> None:
    template = (ROOT / "web/templates/sports_game.html").read_text()
    application = (ROOT / "src/runner_web/main.py").read_text()

    assert "<textarea" not in template
    assert "sportsCommentForm" not in template
    assert "data-sports-comments" not in template
    assert "/api/sports/games/{event_id}/comments" not in application
    assert "/api/sports/comments/{comment_id}" not in application
    assert "SportsCommentPayload" not in application
    assert not (ROOT / "web/static/sports-comments.js").exists()
