from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse

from runner_web import api_credits as credits
from runner_web import api_data, api_limits, db, shared_state
from runner_web import api_payments as pay
from runner_web.api_routes import APIBodyLimit, create_api_routes

MINT = "So11111111111111111111111111111111111111112"
RECIPIENT = "0x1111111111111111111111111111111111111111"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "api.db")
    db.init_db()
    with db.connection() as conn:
        for name in ("alice", "bob"):
            conn.execute(
                "INSERT INTO users(id,username,display_name,status,created_at) "
                "VALUES(?,?,?,'active',?)",
                (name, name, name, credits.stamp()),
            )
    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(shared_state, "_client", lambda: redis)
    monkeypatch.setenv("X402_MODE", "test")
    monkeypatch.setenv("X402_TEST_BASE_PAY_TO", RECIPIENT)
    monkeypatch.delenv("X402_TEST_SOLANA_PAY_TO", raising=False)
    monkeypatch.setenv("X402_PAYMENTS_ENABLED", "1")
    monkeypatch.setenv("API_FRESH_JOBS_ENABLED", "1")
    monkeypatch.setenv("HELIUS_API_KEY", "test-only")
    app = FastAPI()
    app.add_middleware(APIBodyLimit)

    def require_origin(request):
        if request.headers.get("origin") != "https://example.test":
            raise HTTPException(403, "Origin required")

    app.include_router(
        create_api_routes(
            require_user=lambda _: {"id": "alice"},
            require_origin=require_origin,
            enforce_rate=lambda *a, **k: None,
            page=lambda *a: {},
            origin=lambda: "https://example.test",
        )
    )
    client = TestClient(app)
    key = credits.create_key("alice", "tests")
    client.headers["Authorization"] = "Bearer " + key["key"]
    yield client, redis
    client.close()
    redis.close()


def fund(owner="alice", amount=1000):
    with db.connection() as conn:
        credits.account(conn, owner)
        conn.execute("UPDATE api_accounts SET paid_credits=? WHERE user_id=?", (amount, owner))


def test_free_month_day_reset_and_paid_overflow(api):
    at = datetime(2026, 10, 17, tzinfo=UTC)
    for i in range(10):
        credits.reserve("alice", f"job-{i}", "wallet", {}, at=at)
    with pytest.raises(HTTPException) as error:
        credits.reserve("alice", "one-more", "snapshot", {}, at=at)
    assert error.value.status_code == 402
    fund(amount=5)
    row, _ = credits.reserve("alice", "paid-now", "tokens", {}, at=at)
    assert (row["free_cost"], row["paid_cost"]) == (0, 5)
    assert credits.balance("alice", at + timedelta(days=1))["free_credits_available"] == 100
    assert (
        credits.balance("alice", datetime(2026, 11, 1, tzinfo=UTC))["free_month_remaining"] == 1000
    )


def test_monthly_limit_and_parallel_reservations(api):
    with db.connection() as conn:
        credits.account(conn, "alice")
        conn.execute("UPDATE api_accounts SET free_month_used=999 WHERE user_id='alice'")

    def reserve(index):
        try:
            return credits.reserve("alice", str(index), "snapshot", {})[0]["id"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(reserve, range(12)))
    assert sum(isinstance(value, str) for value in results) == 1
    assert results.count(402) == 11


def test_refund_once_and_retry_binding(api):
    row, _ = credits.reserve("alice", "read-one", "wallet", {})
    credits.finish(row["id"], error="failed")
    credits.finish(row["id"], error="failed twice")
    assert credits.balance("alice")["free_day_remaining"] == 100
    same, fresh = credits.reserve("alice", "read-one", "wallet", {})
    assert not fresh and same["status"] == "failed"
    with pytest.raises(HTTPException) as error:
        credits.reserve("alice", "read-one", "wallet", {"address": MINT})
    assert error.value.status_code == 409


def test_saved_reads_replay_once_and_refund_failure(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(api_data, "snapshot", lambda _: {"score": 42})
    client.headers["Idempotency-Key"] = "same-request"
    first = client.get("/api/v1/tokens/test")
    second = client.get("/api/v1/tokens/test")
    assert first.status_code == second.status_code == 200
    assert first.json()["usage"]["credits"] == 1
    assert second.json()["usage"]["credits"] == 0
    assert second.json()["data"] == first.json()["data"]
    assert credits.balance("alice")["free_day_remaining"] == 99
    assert client.get("/api/v1/tokens/different").status_code == 409
    client.headers["Idempotency-Key"] = "missing-record"

    def missing(_):
        raise HTTPException(404, "Missing")

    monkeypatch.setattr(api_data, "snapshot", missing)
    assert client.get("/api/v1/tokens/missing").status_code == 404
    assert credits.balance("alice")["free_day_remaining"] == 99


def test_limit_before_charge_and_shared_keys(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(api_data, "snapshot", lambda _: {"score": 42})
    for _ in range(10):
        assert client.get("/api/v1/tokens/test").status_code == 200
    client.headers["Authorization"] = "Bearer " + credits.create_key("alice", "second")["key"]
    response = client.get("/api/v1/tokens/test")
    assert response.status_code == 429 and response.headers["Retry-After"] == "60"
    assert credits.balance("alice")["free_day_remaining"] == 90


def test_concurrency_and_redis_failure(api, monkeypatch):
    with api_limits.admission("alice", paid=False):
        with pytest.raises(HTTPException) as error:
            with api_limits.admission("alice", paid=False):
                pass
        assert error.value.status_code == 429

    def unavailable():
        raise RuntimeError("Redis offline")

    monkeypatch.setattr(shared_state, "_client", unavailable)
    assert api[0].get("/api/v1/balance").status_code == 503
    assert credits.balance("alice")["free_day_remaining"] == 100


def test_key_storage_revocation_and_origin(api):
    client, _ = api
    assert client.post("/api/developer/keys", json={"name": "new"}).status_code == 403
    key = client.post(
        "/api/developer/keys", json={"name": "new"}, headers={"Origin": "https://example.test"}
    ).json()
    with db.connection() as conn:
        stored = dict(conn.execute("SELECT * FROM api_keys WHERE id=?", (key["id"],)).fetchone())
    assert key["key"] not in json.dumps(stored)
    client.delete("/api/developer/keys/" + key["id"], headers={"Origin": "https://example.test"})
    assert (
        client.get("/api/v1/balance", headers={"Authorization": "Bearer " + key["key"]}).status_code
        == 401
    )
    assert client.get("/api/v1/pricing").json()["credits_per_usdc"] == 1000


class Facilitator:
    calls = 0
    fail = False
    valid = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def get_supported(self):
        return SupportedResponse(
            kinds=[SupportedKind(x402_version=2, scheme="exact", network="eip155:84532")],
            extensions=[],
            signers={},
        )

    def verify(self, *_):
        return VerifyResponse(is_valid=self.valid, payer=RECIPIENT)

    def settle(self, payload, requirements):
        self.calls += 1
        if self.fail:
            raise TimeoutError("Simulated reply loss")
        return SettleResponse(
            success=True,
            payer=RECIPIENT,
            network=requirements.network,
            transaction="0x" + payload.payload["authorization"]["nonce"],
        )


def payment(row, nonce="abcd"):
    return pay.encode(
        {
            "x402Version": 2,
            "accepted": json.loads(row["requirements"])[0],
            "resource": {"url": row["resource"]},
            "payload": {"authorization": {"from": RECIPIENT, "nonce": nonce}, "signature": "test"},
        }
    )


def test_x402_quote_settlement_and_retry(api, monkeypatch):
    service = Facilitator()
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    client, _ = api
    created = client.post(
        "/api/v1/topups", json={"dollars": 5}, headers={"Idempotency-Key": "buy-five"}
    )
    assert created.status_code == 201
    identifier = created.json()["id"]
    row = pay.get_order(identifier, "alice")
    quote = client.post("/api/v1/topups/" + identifier)
    assert quote.status_code == 402
    challenge = json.loads(base64.b64decode(quote.headers["PAYMENT-REQUIRED"]))
    assert challenge["accepts"][0]["amount"] == "5000000"
    assert challenge["accepts"][0]["payTo"] == RECIPIENT
    signature = payment(row)
    result = client.post("/api/v1/topups/" + identifier, headers={"PAYMENT-SIGNATURE": signature})
    assert result.status_code == 200, result.text
    assert result.json()["balance"]["paid_credits"] == 5000
    assert "PAYMENT-RESPONSE" in result.headers
    assert (
        client.post(
            "/api/v1/topups/" + identifier, headers={"PAYMENT-SIGNATURE": signature}
        ).status_code
        == 200
    )
    assert credits.balance("alice")["paid_credits"] == 5000 and service.calls == 1


def test_payment_terms_and_cross_account_replay(api, monkeypatch):
    service = Facilitator()
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    row = pay.order("alice", 1, "order-a", "https://example.test")
    raw = json.loads(base64.b64decode(payment(row)))
    raw["accepted"]["amount"] = "1"
    with pytest.raises(HTTPException) as error:
        pay.settle(row["id"], "alice", pay.encode(raw))
    assert error.value.status_code == 400 and service.calls == 0
    pay.settle(row["id"], "alice", payment(row))
    other = pay.order("bob", 1, "order-b", "https://example.test")
    with pytest.raises(HTTPException) as error:
        pay.settle(other["id"], "bob", payment(other))
    assert error.value.status_code == 409 and credits.balance("bob")["paid_credits"] == 0


def test_settlement_timeout_keeps_order_for_recovery(api, monkeypatch):
    service = Facilitator()
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    row = pay.order("alice", 1, "order-a", "https://example.test")
    service.fail = True
    with pytest.raises(HTTPException) as error:
        pay.settle(row["id"], "alice", payment(row))
    assert error.value.status_code == 503
    assert credits.balance("alice")["paid_credits"] == 0
    assert pay.get_order(row["id"], "alice")["status"] == "pending"
    service.fail = False
    pay.settle(row["id"], "alice", payment(row))
    assert credits.balance("alice")["paid_credits"] == 1000


def test_job_scope_refund_and_cursor_ownership(api, monkeypatch):
    client, _ = api
    fund()
    response = client.post(
        "/api/v1/jobs/token_check", json={"address": MINT}, headers={"Idempotency-Key": "token-job"}
    )
    assert response.status_code == 202
    identifier = response.json()["job_id"]
    assert credits.balance("alice")["paid_credits"] == 975
    blocked = client.post(
        "/api/v1/jobs/token_check",
        json={"address": MINT},
        headers={"Idempotency-Key": "token-job-other"},
    )
    assert blocked.status_code == 429

    def fail(_):
        raise RuntimeError("provider secret should stay private")

    monkeypatch.setattr(api_data, "token_check", fail)
    api_data.run_pending()
    assert credits.balance("alice")["paid_credits"] == 1000
    result = client.get("/api/v1/jobs/" + identifier).json()
    assert result["status"] == "failed" and "secret" not in result["error"]
    client.headers["Authorization"] = "Bearer " + credits.create_key("bob", "test")["key"]
    assert client.get("/api/v1/jobs/" + identifier).status_code == 404


def test_stale_reservations_return_credits_and_receipts_expire(api):
    row, _ = credits.reserve("alice", "stale-read", "wallet", {})
    with db.connection() as conn:
        conn.execute(
            "UPDATE api_requests SET updated_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(minutes=20)).isoformat(), row["id"]),
        )
    credits.prune()
    assert credits.balance("alice")["free_day_remaining"] == 100
    with db.connection() as conn:
        conn.execute(
            "UPDATE api_requests SET updated_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=8)).isoformat(), row["id"]),
        )
    credits.prune()
    with pytest.raises(HTTPException):
        credits.receipt(row["id"], "alice")


def test_official_sdk_signs_the_order(api, monkeypatch):
    from eth_account import Account
    from x402 import x402ClientSync
    from x402.mechanisms.evm.exact.client import ExactEvmScheme
    from x402.schemas import PaymentRequired

    service = Facilitator()
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    row = pay.order("alice", 1, "sdk-order", "https://example.test")
    signer = Account.create()
    client = x402ClientSync()
    client.register("eip155:84532", ExactEvmScheme(signer))
    signed = client.create_payment_payload(PaymentRequired.model_validate(pay.challenge(row)))
    assert signed.resource.url == row["resource"]
    assert signed.payload["authorization"]["to"].lower() == RECIPIENT.lower()
    assert signed.payload["authorization"]["value"] == "1000000"
    assert len(signed.payload["signature"]) == 132
    assert (
        pay.settle(
            row["id"], "alice", pay.encode(signed.model_dump(by_alias=True, exclude_none=True))
        )["status"]
        == "complete"
    )


def test_simultaneous_settlement_adds_credits_once(api, monkeypatch):
    service = Facilitator()
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    row = pay.order("alice", 1, "race-order", "https://example.test")

    def attempt(_):
        try:
            return pay.settle(row["id"], "alice", payment(row))["status"]
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(attempt, range(10)))
    assert "complete" in results and set(results) <= {"complete", 409}
    assert credits.balance("alice")["paid_credits"] == 1000
    assert service.calls == 1


def test_payment_service_cannot_reuse_settlement_for_another_order(api, monkeypatch):
    service = Facilitator()
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    first = pay.order("alice", 1, "first-order", "https://example.test")
    pay.settle(first["id"], "alice", payment(first))
    second = pay.order("bob", 1, "second-order", "https://example.test")
    monkeypatch.setattr(
        service,
        "settle",
        lambda *_: SettleResponse(
            success=True, network="eip155:84532", transaction="0xabcd", payer=RECIPIENT
        ),
    )
    with pytest.raises(HTTPException) as error:
        pay.settle(second["id"], "bob", payment(second, "different"))
    assert error.value.status_code == 409
    assert credits.balance("bob")["paid_credits"] == 0


def test_solana_quote_uses_sdk_asset_and_facilitator_fee_payer(api, monkeypatch):
    from solders.keypair import Keypair

    recipient = str(Keypair().pubkey())
    fee_payer = str(Keypair().pubkey())
    monkeypatch.setenv("X402_TEST_BASE_PAY_TO", "")
    monkeypatch.setenv("X402_TEST_SOLANA_PAY_TO", recipient)
    service = Facilitator()
    network = pay.NETWORKS["test"]["solana"]
    monkeypatch.setattr(
        service,
        "get_supported",
        lambda: SupportedResponse(
            kinds=[
                SupportedKind(
                    x402_version=2, scheme="exact", network=network, extra={"feePayer": fee_payer}
                )
            ],
            extensions=[],
            signers={},
        ),
    )
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    quote = pay.requirements(5)[0]
    assert quote["network"] == network and quote["payTo"] == recipient
    assert quote["amount"] == "5000000" and quote["extra"]["feePayer"] == fee_payer
    assert len(quote["asset"]) >= 32


def test_cursor_page_scope_and_worker_cap(api, monkeypatch):
    fund()
    row, _ = credits.reserve(
        "alice", "wallet-page", "wallet_scan", {"address": MINT, "cursor": None}, paid_only=True
    )
    calls = []
    from runner_web import onchain_wallets

    def collect(address, **kwargs):
        calls.append(kwargs["state"])
        return {
            "address": address,
            "has_more": True,
            "transactions": 1000,
            "oldest_at": "2026-10-01",
            "_state": {"cursor": "next", "complete": False},
        }

    monkeypatch.setattr(onchain_wallets, "collect_wallet", collect)
    api_data.run_pending()
    result = api[0].get("/api/v1/jobs/" + row["id"]).json()
    assert result["result"]["next_cursor"] == row["id"]
    assert "_state" not in result["result"]
    second, _ = credits.reserve(
        "alice",
        "wallet-page-2",
        "wallet_scan",
        {"address": MINT, "cursor": row["id"]},
        paid_only=True,
    )
    api_data.run_pending()
    assert calls == [None, {"cursor": "next", "complete": False}]
    assert credits.receipt(second["id"], "alice")["status"] == "complete"
    assert credits.balance("alice")["paid_credits"] == 800


def test_api_provider_budget_and_read_failure(api, monkeypatch):
    monkeypatch.setenv("API_HELIUS_DAILY_CREDITS", "2")
    calls = []
    monkeypatch.setattr(
        api_data, "rpc_request", lambda body, **kw: calls.append(kw) or {"result": {}}
    )
    api_data.provider_rpc({}, credits=2)
    with pytest.raises(RuntimeError):
        api_data.provider_rpc({}, credits=1)
    assert len(calls) == 1 and calls[0]["lane"] == "other"


def test_saved_list_ranks_before_paging_and_preserves_revision(api):
    with db.connection() as conn:
        payload = {
            "collected_at": "2026-10-17",
            "rows": [
                {"id": "low", "early": {"score": 1}},
                {"id": "high", "early": {"score": 99}},
                {"id": "middle", "early": {"score": 50}},
            ],
        }
        conn.execute(
            "INSERT INTO worker_state(key,value,updated_at) VALUES('memecoins_snapshot',?,?)",
            (json.dumps(payload), credits.stamp()),
        )
    first = api_data.tokens("", 2)
    assert [item["id"] for item in first["items"]] == ["high", "middle"]
    assert api_data.tokens(first["next_cursor"], 2)["items"][0]["id"] == "low"
    with pytest.raises(HTTPException):
        api_data.tokens("old:2", 2)


def test_account_export_delete_and_inflight_payment(api, monkeypatch):
    from runner_web.privacy import delete_user_data, export_user_data

    service = Facilitator()
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    row = pay.order("alice", 1, "delete-order", "https://example.test")
    service.fail = True
    with pytest.raises(HTTPException):
        pay.settle(row["id"], "alice", payment(row))
    with pytest.raises(HTTPException) as error:
        delete_user_data("alice")
    assert error.value.status_code == 409
    service.fail = False
    pay.settle(row["id"], "alice", payment(row))
    exported = export_user_data("alice")
    assert exported["api_accounts"][0]["paid_credits"] == 1000
    assert "key_hash" not in exported["api_keys"][0]
    assert delete_user_data("alice")["deleted"]
    assert api[0].get("/api/v1/balance").status_code == 401
    with db.connection() as conn:
        payment_row = conn.execute("SELECT * FROM api_topups WHERE id=?", (row["id"],)).fetchone()
    assert payment_row["user_id"] is None and payment_row["proof_json"] is None


def test_read_result_expiry_and_failed_payment_are_safe(api, monkeypatch):
    client, redis = api
    monkeypatch.setattr(api_data, "snapshot", lambda _: {"score": 1})
    first = client.get("/api/v1/tokens/test", headers={"Idempotency-Key": "expire-read"}).json()
    redis.delete(shared_state._key("api:result:" + first["usage"]["request_id"]))
    assert (
        client.get("/api/v1/tokens/test", headers={"Idempotency-Key": "expire-read"}).status_code
        == 410
    )
    assert credits.balance("alice")["free_day_remaining"] == 99
    service = Facilitator()
    service.valid = False
    monkeypatch.setattr(pay, "facilitator", lambda: service)
    row = pay.order("alice", 1, "decline-order", "https://example.test")
    with pytest.raises(HTTPException) as error:
        pay.settle(row["id"], "alice", payment(row))
    assert error.value.status_code == 402 and service.calls == 0
    assert credits.balance("alice")["paid_credits"] == 0


def test_openapi_and_payment_configuration(api, monkeypatch):
    schema = api[0].get("/api/v1/openapi.json").json()
    assert "/api/v1/topups/{identifier}" in schema["paths"]
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    monkeypatch.setenv("X402_TEST_BASE_PAY_TO", "invalid")
    assert api[0].get("/api/v1/pricing").json()["payments"]["enabled"] is False
    monkeypatch.setenv("X402_MODE", "production")
    monkeypatch.delenv("CDP_API_KEY_ID", raising=False)
    assert not pay.configuration()["enabled"]


def test_cache_budget_returns_credits(api, monkeypatch):
    monkeypatch.setattr(api_limits, "MAX_CACHE_BYTES", 1)
    monkeypatch.setattr(api_data, "snapshot", lambda _: {"score": 42})
    response = api[0].get("/api/v1/tokens/test")
    assert response.status_code == 503
    assert credits.balance("alice")["free_day_remaining"] == 100


def test_cache_budget_reclaims_expired_results(api, monkeypatch):
    monkeypatch.setattr(api_limits.time, "time", lambda: 1000)
    api_limits.save_result("first", {"data": "first"})
    size = int(api[1].get(shared_state._key("api:result-bytes")))
    assert size > 0
    monkeypatch.setattr(api_limits.time, "time", lambda: 5000)
    api_limits.save_result("second", {"data": "second"})
    assert api[1].hget(shared_state._key("api:result-sizes"), "first") is None
    assert int(api[1].get(shared_state._key("api:result-bytes"))) < size * 2


def test_large_api_body_is_bounded(api):
    response = api[0].post("/api/v1/topups", content=b"X" * 16385)
    assert response.status_code == 413
    response = api[0].post("/api/v1/topups", content=iter([b"X" * 10000, b"Y" * 10000]))
    assert response.status_code == 413


def test_busy_provider_budget_rejects_before_credit_reservation(api, monkeypatch):
    fund()
    monkeypatch.setenv("API_HELIUS_DAILY_CREDITS", "0")
    response = api[0].post(
        "/api/v1/jobs/wallet_scan",
        json={"address": MINT},
        headers={"Idempotency-Key": "capacity-test"},
    )
    assert response.status_code == 429
    assert credits.balance("alice")["paid_credits"] == 1000
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM api_requests").fetchone()["n"] == 0
