from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from runner_web import api_credits, api_data, api_insights, db, memecoin_lookup
from runner_web.memecoin_chain_parser import SOL, USDC
from tests.test_metered_api import MINT, api  # noqa: F401

AT = datetime(2031, 1, 31, 23, 55, tzinfo=UTC)


@pytest.mark.parametrize(
    "value,state,age",
    [
        (AT.isoformat(), "fresh", 0),
        ((AT - timedelta(seconds=900)).isoformat(), "stale", 900),
        ((AT + timedelta(seconds=60)).isoformat(), "fresh", 0),
        ((AT + timedelta(seconds=61)).isoformat(), "unknown", None),
        (None, "unknown", None),
        ("broken", "unknown", None),
        ("2031-01-31T23:55:00", "unknown", None),
    ],
)
def test_freshness_covers_expiry_and_uncertain_timestamps(value, state, age):
    result = api_insights.freshness(value, at=AT)
    assert (result["status"], result["age_seconds"]) == (state, age)
    assert result["evaluated_at"] == AT.isoformat()
    if state == "unknown":
        assert result["fresh_until"] is None


def token():
    return {
        "token_address": MINT,
        "observed_at": AT.isoformat(),
        "early": {
            "version": "memecoin-early-v3",
            "score": 42.5,
            "state": "building",
            "reasons": ["Buyers increased within the saved window"],
        },
        "ratification": {
            "as_of": AT.isoformat(),
            "standards": [
                {"key": "mint_authority", "met": True, "applies": True},
                {
                    "key": "freeze_authority",
                    "met": False,
                    "applies": True,
                    "detail": "still active",
                },
            ],
        },
        "memecoin_assessment": {
            "sentiment": {
                "state": "buying",
                "observed_swaps": 20,
                "net_buyers": 8,
                "net_sellers": 2,
            },
            "coverage": {
                "chain_checked_at": (AT - timedelta(hours=1)).isoformat(),
                "collection_partial": True,
                "recorded_coverage_gaps": 3,
            },
            "risk": {
                "state": "detected",
                "factors": [{"kind": "creator_sell", "signature": "receipt"}],
                "missing_checks": ["Sale proceeds at a stated position size"],
            },
        },
    }


def test_token_report_preserves_reasons_and_distinguishes_pending_checks():
    row = token()
    previous = {
        "observed_at": (AT - timedelta(minutes=5)).isoformat(),
        "features": {"score": 40, "state": "quiet", "version": "memecoin-early-v3"},
    }
    before = copy.deepcopy(row)
    result = api_insights.token_insights(row, None, previous, at=AT)
    assert row == before
    assert result["momentum"]["change"]["score_delta"] == 2.5
    assert result["momentum"]["change"]["elapsed_seconds"] == 300
    assert result["momentum"]["reasons"] == row["early"]["reasons"]
    assert result["checks"]["status"] == "review"
    assert [result["checks"][key] for key in ("met", "flagged", "pending")] == [1, 1, 7]
    assert result["activity"]["freshness"]["status"] == "stale"
    assert result["activity"]["collection_partial"] is True
    assert result["risk_evidence"]["factors"][0]["signature"] == "receipt"
    assert result["controls_freshness"]["status"] == "unknown"


@pytest.mark.parametrize(
    "version,score,status",
    [("older", 2, "model_changed"), (None, 2, "pending"), ("memecoin-early-v3", "NaN", "pending")],
)
def test_score_comparison_requires_matching_models_and_finite_values(version, score, status):
    previous = {
        "observed_at": (AT - timedelta(minutes=5)).isoformat(),
        "features": {"score": score, "version": version},
    }
    result = api_insights.score_change(token(), previous)
    assert result["status"] == status
    assert result["score_delta"] is None


def test_missing_token_data_keeps_checks_and_activity_pending():
    result = api_insights.token_insights({}, None, None, at=AT)
    assert result["checks"]["pending"] == 9
    assert result["checks"]["status"] == "incomplete"
    assert result["activity"]["observed_swaps"] is None
    assert result["momentum"]["score"] is None


def test_wallet_summary_separates_quotes_and_measures_partial_costs():
    data = {
        "updated_at": AT.isoformat(),
        "has_more": True,
        "balance_sol": 5,
        "holdings": [
            {"mint": "a", "value_usd": 600, "cost": 4, "unit": "SOL"},
            {"mint": "b", "value_usd": None, "cost": None},
            {"mint": USDC, "value_usd": 400, "cost": None},
        ],
        "pnl": [
            {"unit": "SOL", "realized": 2, "unrealized": 1, "closed_trades": 2, "unknown_sales": 1},
            {
                "unit": "USDC",
                "realized": 100,
                "unrealized": None,
                "closed_trades": 1,
                "unknown_sales": 0,
            },
        ],
        "trades": [
            {
                "unit": "SOL",
                "side": "Sell",
                "realized": 3,
                "cost_known": True,
                "at": AT.isoformat(),
            },
            {"unit": "SOL", "side": "Sell", "realized": -1, "cost_known": True},
            {"unit": "SOL", "side": "Sell", "realized": None, "cost_known": False},
            {"unit": "USDC", "side": "Sell", "realized": 100, "cost_known": True},
        ],
    }
    result = api_insights.wallet_insights(data, at=AT)
    assert result["coverage"]["history"] == "partial"
    assert result["coverage"]["position_cost_coverage_pct"] == 50
    assert result["exposure"]["priced_value_usd"] == 1000
    assert result["exposure"]["unpriced_holdings"] == 1
    assert result["exposure"]["top_positions"][0]["share_of_priced_value_pct"] == 60
    assert result["exposure"]["native_sol_balance"] == 5
    assert result["pnl_by_quote"][0]["sale_cost_coverage_pct"] == 66.67
    sol, usdc = result["recent_sales"]["by_quote"]
    assert (sol["realized"], sol["win_rate_pct"], sol["unknown_cost_sales"]) == (2, 50, 1)
    assert (usdc["realized"], usdc["win_rate_pct"]) == (100, 100)


def test_empty_unknown_and_nonfinite_wallet_values_stay_distinct():
    result = api_insights.wallet_insights(
        {
            "holdings": [
                {"mint": "a", "value_usd": "NaN", "cost": "NaN"},
                {"mint": "b", "value_usd": "Infinity", "cost": None},
            ]
        },
        at=AT,
    )
    assert result["coverage"]["history"] == "unknown"
    assert result["coverage"]["unknown_cost_positions"] == 2
    assert result["exposure"]["priced_value_usd"] is None
    assert result["exposure"]["top_five_share_pct"] is None
    assert result["recent_sales"]["by_quote"][0]["win_rate_pct"] is None
    assert (
        api_insights.wallet_insights({"has_more": False}, at=AT)["coverage"]["history"]
        == "complete"
    )


def save_token():
    coin_id = "chain-" + api_credits.digest("solana:" + MINT)
    row = {**token(), "id": coin_id}
    with db.connection() as database:
        database.execute(
            "INSERT INTO memecoin_assets VALUES(?,?,?,?)",
            (coin_id, json.dumps(row), AT.isoformat(), "test"),
        )
        for minutes, score in [(-5, 40), (0, 42.5), (5, 99)]:
            stamp = (AT + timedelta(minutes=minutes)).isoformat()
            database.execute(
                "INSERT INTO memecoin_quote_history(coin_id,observed_at,collected_at,price,run_id,"
                "features_json) VALUES(?,?,?,?,?,?)",
                (
                    coin_id,
                    stamp,
                    stamp,
                    1,
                    "test",
                    json.dumps({"score": score, "version": "memecoin-early-v3"}),
                ),
            )
    return coin_id


def test_mint_report_uses_saved_data_and_replays_one_charge(api, monkeypatch):  # noqa: F811
    client, _ = api
    coin_id = save_token()

    def provider_forbidden(*args, **kwargs):
        pytest.fail("Saved report attempted a provider read")

    monkeypatch.setattr(memecoin_lookup, "rpc_request", provider_forbidden)
    monkeypatch.setattr(memecoin_lookup, "_download", provider_forbidden)
    client.headers["Idempotency-Key"] = "token-insights"
    path = f"/api/v1/tokens/by-mint/{MINT}/analysis"
    first, replay = client.get(path), client.get(path)
    assert first.status_code == replay.status_code == 200
    result = first.json()["data"]
    assert result["id"] == coin_id and result["mint"] == MINT
    assert result["insights"]["momentum"]["change"]["score_delta"] == 2.5
    assert first.json()["usage"]["credits"] == 10
    assert replay.json()["usage"]["credits"] == 0
    assert replay.json()["data"] == result
    assert api_credits.balance("alice")["free_day_remaining"] == 90
    client.headers.pop("Idempotency-Key")
    assert client.get("/api/v1/tokens/by-mint/invalid/analysis").status_code == 422
    assert client.get(f"/api/v1/tokens/by-mint/{USDC}/analysis").status_code == 404
    assert api_credits.balance("alice")["free_day_remaining"] == 90
    assert path.replace(MINT, "{mint}") in client.get("/api/v1/openapi.json").json()["paths"]


def save_wallet(data, address=MINT):
    with db.connection() as database:
        database.execute(
            "INSERT INTO onchain_wallet_snapshots(address,payload_json,updated_at,refresh_after) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET payload_json=excluded.payload_json,"
            "updated_at=excluded.updated_at",
            (address, json.dumps(data), data["updated_at"], data["updated_at"]),
        )


def test_wallet_pages_include_whole_snapshot_insights_and_bind_revision(api):  # noqa: F811
    client, _ = api
    # The largest position is outside page one. Summary math covers the whole snapshot.
    data = {
        "updated_at": AT.isoformat(),
        "has_more": False,
        "holdings": [{"mint": str(index), "value_usd": 1, "cost": 0} for index in range(100)]
        + [{"mint": "largest", "value_usd": 900, "cost": None}],
    }
    save_wallet(data)
    first = client.get(f"/api/v1/wallets/{MINT}").json()["data"]
    assert len(first["holdings"]) == 100
    assert first["insights"]["exposure"]["holdings"] == 101
    assert first["insights"]["exposure"]["top_positions"][0]["share_of_priced_value_pct"] == 90
    cursor = first["next_cursor"]
    second = client.get(f"/api/v1/wallets/{MINT}", params={"cursor": cursor})
    assert second.status_code == 200
    assert second.json()["data"]["holdings"][0]["mint"] == "largest"
    assert second.json()["data"]["next_cursor"] is None
    assert api_credits.balance("alice")["free_day_remaining"] == 80
    save_wallet(data, USDC)
    assert client.get(f"/api/v1/wallets/{USDC}", params={"cursor": cursor}).status_code == 409
    data["updated_at"] = (AT + timedelta(minutes=1)).isoformat()
    save_wallet(data)
    assert client.get(f"/api/v1/wallets/{MINT}", params={"cursor": cursor}).status_code == 409
    assert api_credits.balance("alice")["free_day_remaining"] == 80


def test_fresh_wallet_job_includes_summary(api, monkeypatch):  # noqa: F811
    from runner_web import onchain_wallets
    from tests.test_metered_api import fund

    fund()
    monkeypatch.setattr(
        onchain_wallets,
        "collect_wallet",
        lambda *a, **k: {
            "updated_at": AT.isoformat(),
            "has_more": True,
            "holdings": [{"mint": "a", "value_usd": None, "cost": None}],
            "_state": {"book": {}},
        },
    )
    row, _ = api_credits.reserve(
        "alice", "fresh-insights", "wallet_scan", {"address": SOL}, paid_only=True
    )
    api_data.run_pending()
    result = json.loads(api_credits.receipt(row["id"], "alice")["result_json"])
    assert result["insights"]["coverage"]["history"] == "partial"
    assert result["insights"]["exposure"]["unpriced_holdings"] == 1
