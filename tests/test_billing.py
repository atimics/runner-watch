from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import stripe
from fastapi import HTTPException
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import billing, db
from runner_web import main as web_main
from runner_web.billing import delete_customer
from runner_web.db import connection, init_db
from runner_web.flash_wallet import (
    COMMENT_COST,
    PUBLISH_REPORT_REWARD,
    REPORT_COST,
    InsufficientFlashError,
    claim_daily_flash,
    credit_flash,
    spend_flash,
    wallet_for_user,
)
from runner_web.main import (
    APP_ORIGIN,
    billing_checkout_api,
    billing_page,
    claim_daily_flash_api,
    token_hash,
)
from runner_web.product_catalog import roadmap_snapshot


def page_request(path: str, *, method: str = "GET") -> Request:
    request = Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"origin", APP_ORIGIN.encode())] if method == "POST" else [],
            "client": ("127.0.0.1", 4800),
        }
    )
    request.state.csp_nonce = "test"
    return request


def create_user(user_id: str = "wallet-user") -> None:
    timestamp = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            (user_id, user_id.replace("-", "_"), "Wallet User", "active", timestamp),
        )


def test_billing_page_is_now_a_public_flash_wallet() -> None:
    response = billing_page(page_request("/billing"), None)
    html = response.body.decode()

    assert "Flash pays for AI actions." in html
    assert "There is no Pro subscription" in html
    assert "Claim 100 Flash" not in html
    assert "Log in to claim" in html
    assert "Generate today's ticker report" in html
    assert "AI-generated ticker comment" in html
    assert "Win a sports Call" in html
    assert "Stripe checkout is disabled" in html
    assert 'href="/roadmap"' in html

    roadmap = roadmap_snapshot()
    items = [item for group in roadmap["groups"] for item in group["items"]]
    wallet = next(item for item in items if item["key"] == "flash-wallet")
    assert wallet["status"] == "live"
    assert wallet["decision"] == "own"
    assert next(item for item in items if item["key"] == "options")["decision"] == "cut"


def test_daily_flash_must_be_claimed_and_does_not_backfill(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "wallet.db")
    init_db()
    create_user()
    first_day = datetime(2026, 8, 26, 12, tzinfo=UTC)

    empty = wallet_for_user("wallet-user", at=first_day)
    first, claimed = claim_daily_flash("wallet-user", at=first_day)
    duplicate, claimed_again = claim_daily_flash(
        "wallet-user", at=first_day + timedelta(hours=2)
    )
    missed_day = wallet_for_user("wallet-user", at=first_day + timedelta(days=2))
    later, claimed_later = claim_daily_flash("wallet-user", at=first_day + timedelta(days=3))

    assert empty["balance"] == 0
    assert empty["claim_day"] == "2026-08-26"
    assert empty["can_claim"] is True
    assert claimed is True
    assert first["balance"] == 100
    assert first["claimed_today"] is True
    assert claimed_again is False
    assert duplicate["balance"] == 100
    assert missed_day["balance"] == 100
    assert missed_day["can_claim"] is True
    assert claimed_later is True
    assert later["balance"] == 200


def test_signed_in_pages_show_flash_pnl_and_the_release_claim_modal(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "account-strip.db")
    init_db()
    create_user()
    raw_session = "account-strip-session"
    timestamp = datetime.now(UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                token_hash(raw_session),
                "wallet-user",
                timestamp.isoformat(),
                (timestamp + timedelta(days=1)).isoformat(),
            ),
        )
        database.execute(
            "INSERT INTO caller_identities("
            "id,handle,user_id,status,claim_cost_cents,claimed_at) "
            "VALUES(?,?,?,'active',0,?)",
            ("wallet-identity", "steady-wolf", "wallet-user", timestamp.isoformat()),
        )
        database.execute(
            """
            INSERT INTO community_calls(
                id,public_id,user_id,caller_identity_id,ticker,entry_price,entry_at,
                exit_price,exit_at,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,'closed',?,?)
            """,
            (
                "wallet-call",
                "wallet-public-call",
                "wallet-user",
                "wallet-identity",
                "ONE",
                1.0,
                timestamp.isoformat(),
                1.1,
                timestamp.isoformat(),
                timestamp.isoformat(),
                timestamp.isoformat(),
            ),
        )

    response = billing_page(page_request("/billing"), raw_session)
    html = response.body.decode()

    assert 'class="account-strip runners-account-strip"' in html
    assert "Caller PnL" in html
    assert "+10.0%" in html
    assert "1–0 record" in html
    assert 'id="flashReleaseDialog"' in html
    assert "/static/flash-daily-release.webp" in html
    assert "Claim 100 Flash" in html
    assert "window.RatiFlash" in html


def test_flash_spend_and_publish_reward_are_idempotent(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "ledger.db")
    init_db()
    create_user()
    claim_daily_flash("wallet-user")

    with connection() as database:
        report_balance, spent = spend_flash(
            database,
            "wallet-user",
            REPORT_COST,
            kind="report_generation",
            reference_id="report-one",
        )
        duplicate_balance, spent_twice = spend_flash(
            database,
            "wallet-user",
            REPORT_COST,
            kind="report_generation",
            reference_id="report-one",
        )
        rewarded_balance, rewarded = credit_flash(
            database,
            "wallet-user",
            PUBLISH_REPORT_REWARD,
            kind="report_published",
            reference_id="report-one",
        )
        final_balance, rewarded_twice = credit_flash(
            database,
            "wallet-user",
            PUBLISH_REPORT_REWARD,
            kind="report_published",
            reference_id="report-one",
        )

    assert report_balance == 0
    assert spent is True
    assert duplicate_balance == 0
    assert spent_twice is False
    assert rewarded_balance == 50
    assert rewarded is True
    assert final_balance == 50
    assert rewarded_twice is False


def test_flash_spend_rejects_an_empty_wallet(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "empty.db")
    init_db()
    create_user()

    with pytest.raises(InsufficientFlashError, match="costs 10 Flash"):
        with connection() as database:
            spend_flash(
                database,
                "wallet-user",
                COMMENT_COST,
                kind="comment_generation",
                reference_id="comment-one",
            )

    assert wallet_for_user("wallet-user")["balance"] == 0


def test_daily_claim_api_updates_the_signed_in_wallet(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "claim-api.db")
    init_db()
    create_user()
    raw_session = "wallet-session"
    timestamp = datetime.now(UTC)
    with connection() as database:
        database.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (
                token_hash(raw_session),
                "wallet-user",
                timestamp.isoformat(),
                (timestamp + timedelta(days=1)).isoformat(),
            ),
        )

    response = claim_daily_flash_api(
        page_request("/api/flash/claim", method="POST"), raw_session
    )
    payload = json.loads(response.body)

    assert payload["claimed"] is True
    assert payload["wallet"]["balance"] == 100


def test_stripe_checkout_stays_disabled_even_when_secrets_exist(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "stripe-disabled.db")
    init_db()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_runner")
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_pro")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_runner")

    assert billing.billing_config().checkout_ready is False
    with pytest.raises(RuntimeError, match="not configured"):
        billing.create_checkout_session({"id": "wallet-user"}, "https://stonks.test")
    with pytest.raises(HTTPException) as error:
        billing_checkout_api(page_request("/api/billing/checkout", method="POST"), "missing")
    assert error.value.status_code == 401


def test_account_erasure_deletes_the_linked_stripe_customer(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_runner")
    deleted: list[str] = []
    monkeypatch.setattr(
        billing.stripe.Customer,
        "delete",
        lambda customer_id: deleted.append(customer_id)
        or {"id": customer_id, "deleted": True},
    )

    assert delete_customer({"stripe_customer_id": None}) is False
    assert delete_customer({"stripe_customer_id": "cus_delete_me"}) is True
    assert deleted == ["cus_delete_me"]


def test_account_erasure_does_not_hide_a_stripe_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_runner")

    def failed_delete(customer_id: str) -> dict[str, Any]:
        raise stripe.APIConnectionError("Stripe is unavailable")

    monkeypatch.setattr(billing.stripe.Customer, "delete", failed_delete)

    with pytest.raises(stripe.APIConnectionError):
        delete_customer({"stripe_customer_id": "cus_keep_local_until_retry"})


def test_account_deletion_runs_stripe_before_local_erasure(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(web_main, "require_origin", lambda request: None)
    monkeypatch.setattr(web_main, "enforce_rate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_main,
        "require_user",
        lambda session: {"id": "delete-user", "stripe_customer_id": "cus_delete"},
    )
    monkeypatch.setattr(
        web_main,
        "delete_customer",
        lambda user: calls.append("stripe") or True,
    )
    monkeypatch.setattr(
        web_main,
        "delete_user_data",
        lambda user_id: calls.append("local") or {"deleted": True},
    )

    response = web_main.account_delete_api(
        web_main.AccountDeletePayload(confirmation="DELETE MY ACCOUNT"),
        page_request("/api/account/delete", method="POST"),
        "delete-session",
    )

    assert calls == ["stripe", "local"]
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_account_deletion_keeps_local_data_when_stripe_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    local_called = False
    monkeypatch.setattr(web_main, "require_origin", lambda request: None)
    monkeypatch.setattr(web_main, "enforce_rate", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_main,
        "require_user",
        lambda session: {"id": "retry-user", "stripe_customer_id": "cus_retry"},
    )

    def failed_stripe(user: dict[str, Any]) -> bool:
        raise stripe.APIConnectionError("Stripe is unavailable")

    def local_delete(user_id: str) -> dict[str, bool]:
        nonlocal local_called
        local_called = True
        return {"deleted": True}

    monkeypatch.setattr(web_main, "delete_customer", failed_stripe)
    monkeypatch.setattr(web_main, "delete_user_data", local_delete)

    with pytest.raises(HTTPException) as error:
        web_main.account_delete_api(
            web_main.AccountDeletePayload(confirmation="DELETE MY ACCOUNT"),
            page_request("/api/account/delete", method="POST"),
            "retry-session",
        )

    assert error.value.status_code == 502
    assert local_called is False
