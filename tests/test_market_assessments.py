from datetime import UTC, datetime

import pytest

from runner_web.market_screens import detail, listing, row


def coin(**extra):
    return {"id": "chain-test", "symbol": "TEST", "token_address": "token-a", **extra}


def game(**extra):
    return {
        "id": "nba:test",
        "status": "pre",
        "home_team_name": "Home",
        "away_team_name": "Away",
        "start_time": "2099-01-01T12:00:00Z",
        **extra,
    }


def prediction(**extra):
    return {
        "selection": "home",
        "signal": "lean",
        "home_probability": 0.6,
        "home_market_probability": 0.57,
        "edge": 0.03,
        "observed_at": "2026-09-19T12:00:00Z",
        "evidence": ["Season records"],
        "risks": ["Baseline model"],
        **extra,
    }


def test_saved_coin_score_and_contributions_reach_list_and_detail():
    source = coin(
        score=54,
        score_as_of="2026-09-19T12:00:00Z",
        stage="BUILDING",
        score_detail={
            "drivers": [{"key": "scan", "label": "Scan", "value": 64}],
            "penalties": [{"key": "risk", "label": "Risk", "value": -10}],
        },
    )
    listed = listing("memecoins", [source])["rows"][0]
    opened = detail("memecoins", {"coin": source})["item"]
    assert listed == opened
    assert listed["score"] == 54
    assert listed["tag"] == "SETUP"
    assert listed["assessment"]["contributions"][1]["value"] == -10
    assert listed["score_as_of"] == source["score_as_of"]


def test_coin_liquidity_and_price_move_preserve_unknown_assessment():
    result = row("memecoins", coin(liquidity_usd=1000000, change_24h=1000))
    assert result["score"] is None
    assert result["tag"] == ""
    assert result["assessment"]["status"] == "unknown"
    assert result["assessment"]["reason"]
    assert result["assessment"]["contributions"] == []


def test_saved_coin_quote_is_named_without_inventing_a_score():
    result = row("memecoins", coin(price=0.01, observed_at="2026-09-19T12:00:00Z"))
    assert result["score"] is None
    assert result["tag"] == ""
    assert result["assessment"]["status"] == "quote"
    assert result["assessment"]["label"] == "Market quote"
    assert result["assessment"]["value"] is None


def test_coin_findings_have_sources_and_keep_token_scope():
    findings = [
        {
            "kind": "liquidity_withdrawal",
            "title": "Wallet withdrew pool liquidity",
            "token_address": "token-a",
            "observed_at": "2026-09-19T12:00:00Z",
            "source_url": "https://example.test/tx/1",
            "basis": "observation",
        },
        {"kind": "creator_sell", "title": "Other coin", "token_address": "token-b"},
    ]
    result = detail("memecoins", {"coin": coin(), "findings": findings})["item"]["assessment"]
    assert result["score"] is None
    assert len(result["drivers"]) == 1
    assert result["drivers"][0]["source_url"] == "https://example.test/tx/1"
    assert result["event_impacts"][0]["score_impact"] is None
    assert result["status"] == "evidence"
    assert result["label"] == "Chain evidence"


def test_unknown_token_identity_keeps_findings_out_of_coin_assessment():
    result = row(
        "memecoins",
        {
            "id": "bonk",
            "symbol": "BONK",
            "findings": [{"token_address": "token-a", "title": "Other token"}],
        },
    )
    assert result["assessment"]["drivers"] == []


def test_paused_quote_retains_dated_saved_score_and_assessment_state():
    result = row(
        "memecoins", coin(stale=True, score=20, score_as_of="2026-09-18", trade_state="AVOID")
    )
    assert result["tag"] == "AVOID"
    assert result["change"] == "Price paused"
    assert result["score"] == 20
    assert result["assessment"]["tag"] == "AVOID"
    assert result["assessment"]["freshness"] == "paused"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "bad"])
def test_invalid_saved_score_stays_unknown(value):
    assert row("memecoins", coin(score=value))["score"] is None


def test_sports_probability_is_separate_from_score_and_event_status():
    source = game(prediction=prediction())
    listed = row("sports", source)
    opened = detail("sports", source)["item"]
    assert listed == opened
    assert listed["score"] is None
    assert listed["score_detail"] is None
    assert listed["tag"] == "LEAN"
    assert listed["assessment"]["value"] == 60
    assert listed["assessment"]["unit"] == "%"
    assert listed["assessment"]["selected_team_label"] == "Home"
    assert listed["assessment"]["selected_model_percent"] == 60
    assert listed["assessment"]["selected_market_percent"] == 57
    assert listed["assessment"]["edge_pp"] == 3
    assert listed["assessment"]["contributions"] == []
    assert listed["assessment"]["drivers"][2]["value"] == 3
    assert listed["assessment"]["drivers"][2]["unit"] == "pp"
    assert listed["event_status"] != "LEAN"


def test_sports_pass_keeps_selection_and_missing_probability_honest():
    result = row("sports", game(prediction=prediction(selection="pass", signal="pass")))
    assert result["tag"] == "PASS"
    assert result["assessment"]["value"] is None
    assert result["score"] is None


def test_sports_score_comes_only_from_saved_score():
    result = row("sports", game(score=72, prediction=prediction()))
    assert result["score"] == 72
    assert result["assessment"]["value"] == 72
    assert result["assessment"]["unit"] == "pts"
    assert result["assessment"]["label"] == "Runner score"
    assert result["assessment"]["drivers"][0]["value"] == 60


@pytest.mark.parametrize("state, expected", [("in", "In progress"), ("post", "Final")])
def test_golf_list_detail_use_same_canonical_status(state, expected):
    source = {
        "id": "golf:test",
        "status": state,
        "status_detail": "Scheduled",
        "start_time": datetime.now(UTC).isoformat(),
        "leaderboard": [{"player_name": "Player", "score": -20}],
    }
    listed = row("sports", source)
    opened = detail("sports", source)["item"]
    assert listed["change"] == opened["change"] == expected
    assert listed["value"] == opened["value"] == "-20"
    assert listed["assessment"]["status"] == "unknown"


@pytest.mark.parametrize(
    "url", ["javascript:alert(1)", "//evil.test/path", "https://[broken", "/account/delete"]
)
def test_finding_source_urls_are_safe(url):
    result = row("memecoins", coin(findings=[{"token_address": "token-a", "source_url": url}]))
    assert result["assessment"]["drivers"][0]["source_url"] is None


def test_coin_identity_is_compact():
    address = "A" * 44
    result = row("memecoins", coin(token_address=address, name="solana · " + address))
    assert result["subtitle"] == "AAAAAA…AAAA"


def test_coin_assessment_limits_findings_to_latest_three():
    result = row(
        "memecoins",
        coin(
            findings=[
                {"token_address": "token-a", "observed_at": f"2026-09-19T12:00:0{i}Z"}
                for i in range(6)
            ]
        ),
    )
    assert len(result["assessment"]["drivers"]) == 3
    assert result["assessment"]["drivers"][0]["observed_at"].endswith("05Z")


def test_saved_sports_risk_state_overrides_model_signal():
    result = row("sports", game(rug_level="high", prediction=prediction()))
    assert result["tag"] == "AVOID"
    assert result["risk"] is True
    assert result["assessment"]["tag"] == "AVOID"


def test_saved_coin_score_remains_primary_when_chain_evidence_is_present():
    result = row(
        "memecoins",
        coin(
            score=54,
            score_as_of="2026-09-18T12:00:00Z",
            findings=[
                {
                    "token_address": "token-a",
                    "title": "Saved observation",
                    "observed_at": "2026-09-19T12:00:00Z",
                }
            ],
        ),
    )["assessment"]
    assert (result["label"], result["value"], result["unit"]) == ("Runner score", 54, "pts")
    assert result["as_of"] == "2026-09-18T12:00:00Z"
    assert result["drivers"][0]["observed_at"] == "2026-09-19T12:00:00Z"


def test_sports_model_reason_identifies_team_and_saved_time():
    result = row("sports", game(prediction=prediction()))["assessment"]
    assert result["reason"] == "Home · saved Sep 19, 12:00 UTC"


@pytest.mark.parametrize("key", ["bad);color:red", "<script>", "123", "with space", ""])
def test_saved_score_contribution_keys_are_safe_css_identifiers(key):
    result = row(
        "memecoins",
        coin(
            score=10, score_detail={"drivers": [{"key": key, "label": "Saved driver", "value": 10}]}
        ),
    )["assessment"]
    assert result["contributions"][0]["key"] == "saved"
    assert result["contributions"][0]["label"] == "Saved driver"


@pytest.mark.parametrize("url", ["https://example.test/\npath", "https://example.test/\\path"])
def test_finding_source_urls_reject_browser_normalization_characters(url):
    result = row("memecoins", coin(findings=[{"token_address": "token-a", "source_url": url}]))
    assert result["assessment"]["drivers"][0]["source_url"] is None
