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


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    robinhood_chain.reset_cache()
    monkeypatch.delenv("ROBINHOOD_CHAIN_ENABLED", raising=False)
    yield
    robinhood_chain.reset_cache()


def _transport(payload):
    return lambda url, headers, timeout: payload


def test_enabled_lookup_returns_the_chain_4663_token(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    robinhood_chain.refresh_assets(transport=_transport(assets_payload()), now=1000.0)

    token = robinhood_chain.stock_token("p", now=1000.0)

    assert token is not None
    assert token["symbol"] == "P"
    assert token["contract_address"] == CONTRACT
    assert token["chain_id"] == 4663
    assert token["status"] == "active"
    assert token["docs_url"] == robinhood_chain.DOCS_URL


def test_prefers_the_chain_4663_deployment_and_normalises_status(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    robinhood_chain.refresh_assets(transport=_transport(assets_payload()), now=1000.0)

    token = robinhood_chain.stock_token("APLD", now=1000.0)

    assert token["contract_address"] == "0xb8DBf92F9741c9ac1c32115E78581f23509916FD"
    assert token["status"] == "inactive"
    assert token["multiplier"] == "2.0"


def test_disabled_switch_returns_nothing(monkeypatch):
    robinhood_chain.refresh_assets(transport=_transport(assets_payload()), now=1000.0)

    assert robinhood_chain.stock_token("P", now=1000.0) is None


def test_unknown_ticker_and_unusable_payload_return_nothing(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    robinhood_chain.refresh_assets(transport=_transport(assets_payload()), now=1000.0)
    assert robinhood_chain.stock_token("NOPE", now=1000.0) is None

    robinhood_chain.reset_cache()
    robinhood_chain.refresh_assets(transport=_transport({"assets": []}), now=1000.0)
    assert robinhood_chain.stock_token("P", now=1000.0) is None


def test_refresh_failure_keeps_the_last_snapshot(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_CHAIN_ENABLED", "1")
    robinhood_chain.refresh_assets(transport=_transport(assets_payload()), now=1000.0)

    def failing(url, headers, timeout):
        raise OSError("network down")

    robinhood_chain.refresh_assets(transport=failing, now=2000.0)

    token = robinhood_chain.stock_token("P", now=1000.0)
    assert token is not None and token["contract_address"] == CONTRACT
