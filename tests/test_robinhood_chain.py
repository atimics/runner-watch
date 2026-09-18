from __future__ import annotations

import pytest

from runner_web import robinhood_chain

CONTRACT = "0x1Cdad396DB64BDa184d5182A97Dd9B3C62100b7D"


def assets_payload():
    return {
        "assets": [
            {
                "id": "0x0000000000000000000000000000000002c4d1ce31ec4310b2c507c921a52b70",
                "tokenSymbol": "P",
                "tokenName": "Everpure • Robinhood Token",
                "deployments": [{"contractAddress": CONTRACT, "chainId": 4663}],
                "currentMultiplier": "1.000000000000000000",
                "pendingMultiplier": "",
                "logoUrl": "https://cdn.robinhood.com/ncw_assets/logos/0x1cdad.png",
                "status": "ASSET_STATUS_ACTIVE",
            },
            {
                "tokenSymbol": "APLD",
                "tokenName": "Applied Digital • Robinhood Token",
                "deployments": [
                    {"contractAddress": "0x0000000000000000000000000000000000000001", "chainId": 1},
                    {
                        "contractAddress": "0xb8DBf92F9741c9ac1c32115E78581f23509916FD",
                        "chainId": 4663,
                    },
                ],
                "currentMultiplier": "2.0",
                "status": "ASSET_STATUS_INACTIVE",
            },
        ]
    }


def prices_payload():
    return {
        "quotes": [
            {
                "tokenSymbol": "P",
                "bid": "213.45",
                "ask": "213.47",
                "currency": "USD",
                "dailyTradingVolume": "48293710",
                "isTradingHalt": False,
                "generatedAt": "2026-06-23T15:53:30Z",
            }
        ]
    }


def actions_payload():
    return {
        "corpActions": [
            {
                "id": "0xabc",
                "type": "CORPORATE_ACTION_TYPE_FORWARD_SPLIT",
                "status": "CORPORATE_ACTION_STATUS_COMPLETED",
                "processDate": {"year": 2026, "month": 6, "day": 15},
                "tokenSymbol": "P",
                "deployments": [{"contractAddress": CONTRACT, "chainId": 4663}],
                "details": {"forwardSplit": {"oldRate": "1", "newRate": "4"}},
            }
        ]
    }


def fake_transport(url, headers, timeout):
    if url.endswith("/assets"):
        return assets_payload()
    if "/prices/" in url:
        return prices_payload()
    if url.endswith("/corporate-actions"):
        return actions_payload()
    return {}


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    robinhood_chain.reset_cache()
    robinhood_chain.set_transport(fake_transport)
    monkeypatch.delenv("ROBINHOOD_CHAIN_ENABLED", raising=False)
    yield
    robinhood_chain.reset_cache()


def _refresh(now=1000.0):
    robinhood_chain.refresh_assets(now=now)
    robinhood_chain.refresh_price("P", now=now)
    robinhood_chain.refresh_actions(now=now)


def test_enabled_lookup_returns_the_chain_4663_token(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    _refresh()

    token = robinhood_chain.stock_token("p", now=1000.0)

    assert token is not None
    assert token["symbol"] == "P"
    assert token["contract_address"] == CONTRACT
    assert token["chain_id"] == 4663
    assert token["status"] == "active"
    assert token["docs_url"] == robinhood_chain.DOCS_URL


def test_token_carries_quote_and_corporate_actions(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    _refresh()

    token = robinhood_chain.stock_token("P", now=1000.0)

    assert token["price"]["bid"] == "213.45"
    assert token["price"]["ask"] == "213.47"
    assert token["price"]["halt"] is False
    assert token["actions"][0]["label"] == "Forward split"
    assert token["actions"][0]["date"] == "2026-06-15"


def test_prefers_the_chain_4663_deployment_and_normalises_status(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    _refresh()

    token = robinhood_chain.stock_token("APLD", now=1000.0)

    assert token["contract_address"] == "0xb8DBf92F9741c9ac1c32115E78581f23509916FD"
    assert token["status"] == "inactive"
    assert token["multiplier"] == "2.0"


def test_disabled_switch_returns_nothing(monkeypatch):
    _refresh()

    assert robinhood_chain.stock_token("P", now=1000.0) is None
    assert robinhood_chain.price_for("P", now=1000.0) is None
    assert robinhood_chain.actions_for("P", now=1000.0) == []


def test_unknown_ticker_and_unusable_payload_return_nothing(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    _refresh()
    assert robinhood_chain.stock_token("NOPE", now=1000.0) is None

    robinhood_chain.reset_cache()
    robinhood_chain.set_transport(fake_transport)
    robinhood_chain.refresh_assets(transport=lambda url, headers, timeout: {"assets": []})
    assert robinhood_chain.stock_token("P", now=1000.0) is None


def test_refresh_failure_keeps_the_last_snapshot(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    _refresh()

    def failing(url, headers, timeout):
        raise OSError("network down")

    robinhood_chain.refresh_assets(transport=failing, now=2000.0)
    robinhood_chain.refresh_price("P", transport=failing, now=2000.0)

    token = robinhood_chain.stock_token("P", now=1000.0)
    assert token is not None and token["contract_address"] == CONTRACT
    assert token["price"]["bid"] == "213.45"
