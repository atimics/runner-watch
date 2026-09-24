import json

import pytest
from fastapi.testclient import TestClient

from runner_web import db, main
from runner_web.cluster_worth import _saved_prices, cluster_summary, cluster_worth
from tests.test_entity_view import event
from tests.test_stock_map import filing_row, insert


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "cluster.db")
    db.init_db()
    with main.PUBLIC_SCREEN_DATA_CONDITION:
        main.PUBLIC_SCREEN_DATA_CACHE.clear()


def holding(accession, ticker, cik=101, shares=100, **changes):
    return filing_row(
        accession,
        ticker=ticker,
        actor_cik=cik,
        evidence_json=json.dumps(
            {
                "owners": [{"cik": cik, "name": f"Fund {cik}"}],
                "transactions": [
                    {
                        "line": 1,
                        "security": "Common stock",
                        "security_type": "nonDerivative",
                        "code": "P",
                        "direction": "A",
                        "ownership": "D",
                        "post_shares": shares,
                    }
                ],
            }
        ),
        **changes,
    )


def quote(ticker, price, stamp="2026-09-01T18:00:00+00:00", status="ok"):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO ticker_quotes(ticker,price,observed_at,status,source,requested_at) "
            "VALUES(?,?,?,?,?,?)",
            (ticker, price, stamp, status, "test", stamp),
        )


def test_cluster_sums_each_entity_stock_once_and_preserves_unknown_and_zero():
    people = [{"id": "sec:101", "name": "First"}, {"id": "sec:102", "name": "Second"}]
    rows = [
        event(ticker="USO"),
        event("b:1", "OTHER", post_shares=40),
        event("c:1", "USO", post_shares=80, filed_at="2026-09-02"),
        event("d:1", "USO", post_shares=20, people=[people[1]]),
        event("e:1", "ZERO", post_shares=0),
        event("f:1", "MISSING", post_shares=None),
        event("g:1", "JOINT", joint=True, people=people),
        event("h:1", "OPTION", security_type="derivative"),
        event("i:1", "UNRELATED", people=[{"id": "sec:103"}]),
    ]
    prices = [{"ticker": symbol, "price": 10} for symbol in {row["ticker"] for row in rows}]
    result = cluster_summary("USO", people + people, rows + rows, prices)
    assert result["value"] == 1400  # 80 USO + 40 OTHER + 20 USO; every pair once.
    assert result["entity_count"] == 2
    assert (result["covered"], result["tracked"], result["stock_count"]) == (4, 8, 6)
    stocks = {stock["ticker"]: stock for stock in result["stocks"]}
    assert stocks["USO"] == {"ticker": "USO", "value": 1000, "covered": 2, "tracked": 2}
    assert stocks["ZERO"]["value"] == 0
    assert stocks["JOINT"]["value"] is None
    assert stocks["MISSING"]["value"] is None
    assert [member["value"] for member in result["members"]] == [1200, 200]


def test_no_prices_and_empty_cluster_remain_unknown():
    assert cluster_summary("USO", [], [], [])["value"] is None
    result = cluster_summary("USO", [{"id": "sec:101", "name": "Fund"}], [event()], [])
    assert result["value"] is None
    assert result["covered"] == 0
    assert result["tracked"] == 1


def test_cluster_reads_full_portfolios_beyond_map_pages_and_matches_exact_ids(database):
    # The first entity is older than the stock map's first page of 50 filings.
    insert(holding("root", "USO", filed_at="2026-08-01"))
    for i in range(51):
        insert(holding(f"root-{i:03}", "USO", cik=102, shares=20))
    for i in range(201):
        insert(holding(f"repeat-{i:03}", "OTHER", shares=30))
    # This holding is older than the entity map's 200-candidate page.
    insert(holding("old", "OLDER", shares=40, filed_at="2026-07-01"))
    # Text candidates and holders of OTHER alone stay outside USO's cluster.
    insert(holding("false", "FALSE", cik=1101))
    insert(holding("third", "OTHER", cik=103, shares=50000))
    for symbol in ["USO", "OTHER", "OLDER", "FALSE"]:
        quote(symbol, 10)
    result = cluster_worth("USO")
    assert result["value"] == 1900
    assert (result["entity_count"], result["stock_count"], result["covered"]) == (2, 3, 4)
    assert {member["id"] for member in result["members"]} == {"sec:101", "sec:102"}
    response = TestClient(main.app).get("/api/stocks/USO/cluster-worth")
    assert response.status_code == 200
    assert response.json() == result
    assert response.headers["etag"]


def test_named_entities_stay_ticker_scoped_and_positions_link_exact_ciks(database):
    insert(holding("named", "USO", cik=None))
    insert(holding("same-name", "OTHER", cik=None))
    insert(
        filing_row(
            "stake",
            "USO",
            form="SC 13G",
            evidence_json=json.dumps(
                {"positions": [{"name": "Fund", "cik": 991, "shares": 30, "security": "Shares"}]}
            ),
        )
    )
    insert(holding("portfolio", "THIRD", cik=991, shares=20))
    for symbol in ["USO", "OTHER", "THIRD"]:
        quote(symbol, 10)
    result = cluster_worth("USO")
    assert result["value"] == 1500
    assert {stock["ticker"] for stock in result["stocks"]} == {"USO", "THIRD"}


def test_saved_prices_choose_latest_valid_quote_clock(database):
    quote("USO", 10)
    quote("FUTURE", 20, "2999-01-01T00:00:00+00:00")
    quote("INVALID", -5)
    quote("ERROR", 20, status="error")
    quote("UNDATED", 20, "invalid")
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO scan_snapshots(id,ticker,score,stage,session,price,change_pct,"
            "momentum_5m_pct,momentum_15m_pct,breakout_pct,dollar_volume,quote_time,"
            "signals_json,risks_json,captured_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "scan",
                "USO",
                0,
                "watch",
                "regular",
                12,
                0,
                0,
                0,
                0,
                0,
                "2026-09-02T18:00:00+00:00",
                "[]",
                "[]",
                "2026-09-02T18:01:00+00:00",
            ),
        )
        marks = _saved_prices(conn, ["USO", "FUTURE", "INVALID", "ERROR", "UNDATED"])
        assert marks == [{"ticker": "USO", "price": 12, "observed_at": "2026-09-02T18:00:00+00:00"}]
        assert _saved_prices(conn, []) == []
