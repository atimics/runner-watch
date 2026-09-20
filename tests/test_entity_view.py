from runner_web.entity_view import entity_view


def event(id="a:1", ticker="TEST", **values):
    return {
        "id": id,
        "accession": id.split(":")[0],
        "ticker": ticker,
        "filed_at": "2026-09-01",
        "occurred_at": "2026-08-30",
        "people": [{"id": "sec:101"}],
        "security_type": "nonDerivative",
        "security": "Common stock",
        "ownership": "D",
        "view": "activity",
        "post_shares": 100,
        "value": 1000000,
        **values,
    }


def test_worth_uses_holdings_replaces_snapshots_and_keeps_event_lines():
    rows = [
        event(),
        event("a:2", post_shares=80),
        event("b:1", post_shares=50, filed_at="2026-09-02"),
    ]
    result = entity_view(rows, [{"ticker": "TEST", "price": 10}], "sec:101")
    assert result["value"] == 500
    assert [point["value"] for point in result["series"]] == [800, 500]
    assert len(result["stocks"]) == 1 and len(result["stocks"][0]["events"]) == 3


def test_distinct_positions_and_explicit_zero_holdings():
    rows = [event(), event("a:2", ownership="I", ownership_detail="Trust", post_shares=40)]
    result = entity_view(rows, [{"ticker": "TEST", "price": 10}], "sec:101")
    assert result["value"] == 1400
    rows.append(event("b:1", post_shares=0, filed_at="2026-09-02"))
    assert entity_view(rows, [{"ticker": "TEST", "price": 10}], "sec:101")["value"] == 0


def test_coverage_excludes_trades_without_holdings_derivatives_joint_and_ambiguous_classes():
    rows = [
        event(),
        event("b:1", "MISSING", post_shares=None),
        event("c:1", "OPTION", security_type="derivative"),
        event("d:1", "JOINT", joint=True),
        event("e:1", "CLASS", security="Class B Common stock"),
        event("f:1", "UNPRICED"),
        event("g:1", "INVALID", post_shares=float("nan")),
    ]
    prices = [{"ticker": row["ticker"], "price": 10} for row in rows[:-2]]
    result = entity_view(rows, prices, "sec:101")
    assert result["value"] == 1000
    assert result["covered"] == 1 and result["tracked"] == 7
    assert len(result["stocks"]) == 7


def test_stakes_replace_trade_snapshots_and_missing_data_stays_unknown():
    rows = [event(), event("b:1", filed_at="2026-09-02", view="ownership", shares=30)]
    assert entity_view(rows, [{"ticker": "TEST", "price": 10}], "sec:101")["value"] == 300
    assert entity_view(rows, [], "sec:101")["value"] is None
    assert entity_view(rows, [], "sec:102")["stocks"] == []
