from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import billing, db
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
    assert "Generate a Flash report" in html
    assert "AI-generated ticker comment" in html
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

    assert report_balance == 90
    assert spent is True
    assert duplicate_balance == 90
    assert spent_twice is False
    assert rewarded_balance == 140
    assert rewarded is True
    assert final_balance == 140
    assert rewarded_twice is False


def test_flash_spend_rejects_an_empty_wallet(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "empty.db")
    init_db()
    create_user()

    with pytest.raises(InsufficientFlashError, match="costs 5 Flash"):
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
