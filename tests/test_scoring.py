from runner_watch.scoring import ScoreInput, score_runner


def make_input(**overrides: float | None) -> ScoreInput:
    values = {
        "change_pct": 5.0,
        "momentum_5m_pct": 1.8,
        "momentum_15m_pct": 4.0,
        "relative_volume": 3.5,
        "recent_relative_volume": 5.0,
        "breakout_pct": 1.0,
        "range_position": 0.9,
        "dollar_volume": 2_000_000,
        "stale_minutes": 2.0,
    }
    values.update(overrides)
    return ScoreInput(**values)


def test_strong_unextended_move_is_ranked_early() -> None:
    result = score_runner(make_input())
    assert result.score >= 58
    assert result.stage == "EARLY"
    assert "above prior high" in result.signals


def test_extended_move_gets_warning_and_penalty() -> None:
    normal = score_runner(make_input())
    extended = score_runner(make_input(change_pct=28.0, momentum_5m_pct=8.0, momentum_15m_pct=15.0))
    assert extended.stage == "EXTENDED"
    assert "already extended" in extended.risks
    assert extended.score < normal.score + 25


def test_stale_quote_lowers_score() -> None:
    fresh = score_runner(make_input(stale_minutes=1.0))
    stale = score_runner(make_input(stale_minutes=90.0))
    assert stale.score < fresh.score * 0.5
    assert any("old" in risk for risk in stale.risks)


def test_missing_volume_history_is_visible() -> None:
    result = score_runner(make_input(relative_volume=None, recent_relative_volume=None))
    assert "not enough volume history" in result.risks


def test_fading_move_scores_below_healthy_structure() -> None:
    healthy = score_runner(
        make_input(
            momentum_acceleration_pct=1.5,
            vwap_position_pct=2.0,
            pullback_from_high_pct=0.5,
            close_location=0.9,
            recent_dollar_volume=600_000,
        )
    )
    fading = score_runner(
        make_input(
            momentum_5m_pct=-2.0,
            momentum_15m_pct=-1.0,
            momentum_acceleration_pct=-2.0,
            vwap_position_pct=-3.0,
            pullback_from_high_pct=6.0,
            close_location=0.1,
            recent_dollar_volume=600_000,
        )
    )
    assert fading.score < healthy.score - 20
    assert "short-term momentum is falling" in fading.risks
    assert "below VWAP" in fading.risks
