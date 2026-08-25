from runner_watch.risk import RiskInput, assess_risk


def make_input(**overrides: object) -> RiskInput:
    values: dict[str, object] = {
        "setup_score": 70.0,
        "price": 2.0,
        "change_pct": 5.0,
        "momentum_5m_pct": 1.0,
        "momentum_15m_pct": 3.0,
        "vwap_position_pct": 1.0,
        "pullback_from_high_pct": 1.0,
        "close_location": 0.8,
        "dollar_volume": 2_000_000,
        "recent_dollar_volume": 300_000,
        "stale_minutes": 2.0,
    }
    values.update(overrides)
    return RiskInput(**values)  # type: ignore[arg-type]


def test_strong_setup_can_still_be_blocked_as_a_rug() -> None:
    result = assess_risk(
        make_input(
            filing_form="S-3",
            filing_sentiment="risk",
            shares_growth_pct=80.0,
            cash_runway_months=2.0,
        )
    )
    assert result.rug_level == "CRITICAL"
    assert result.trade_state == "AVOID"
    assert any("exit-liquidity" in reason for reason in result.risk_reasons)
    assert any("treasury" in reason for reason in result.risk_reasons)


def test_unconfirmed_crash_is_a_dead_cat_watch() -> None:
    result = assess_risk(
        make_input(
            drawdown_90d_pct=66.0,
            drawdown_52w_pct=72.0,
            rebound_from_20d_low_pct=15.0,
            vwap_position_pct=-1.5,
            momentum_15m_pct=0.5,
        )
    )
    assert result.crash_candidate is True
    assert result.trade_state == "WATCH"
    assert any("dead-cat" in reason for reason in result.risk_reasons)


def test_confirmed_crash_can_trigger_when_rug_risk_is_low() -> None:
    result = assess_risk(
        make_input(
            drawdown_90d_pct=64.0,
            drawdown_52w_pct=68.0,
            rebound_from_20d_low_pct=5.0,
        )
    )
    assert result.rug_level == "LOW"
    assert result.trade_state == "TRIGGERED"


def test_prior_trigger_exits_when_structure_breaks() -> None:
    result = assess_risk(
        make_input(
            previous_trade_state="TRIGGERED",
            vwap_position_pct=-2.0,
            momentum_15m_pct=-4.0,
        )
    )
    assert result.trade_state == "EXIT"


def test_institutional_majority_is_not_an_automatic_bullish_discount() -> None:
    baseline = assess_risk(make_input())
    majority_owned = assess_risk(make_input(institutional_ownership_pct=82.0))
    assert majority_owned.rug_score == baseline.rug_score
