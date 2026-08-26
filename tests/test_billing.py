from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch
from starlette.requests import Request

from runner_web import billing, db
from runner_web import main as web_main
from runner_web.billing import (
    billing_account,
    create_checkout_session,
    create_portal_session,
    price_summary,
    process_webhook_event,
)
from runner_web.db import connection, init_db
from runner_web.main import billing_page, roadmap_page
from runner_web.product_catalog import roadmap_snapshot


def page_request(path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [],
            "client": ("127.0.0.1", 4800),
        }
    )
    request.state.csp_nonce = "test"
    return request


def post_request(path: str) -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "client": ("127.0.0.1", 4801),
        }
    )
    request.state.csp_nonce = "test"
    return request


def test_billing_page_and_roadmap_are_public() -> None:
    response = billing_page(page_request("/billing"), None, None)
    html = response.body.decode()

    assert "Research that shows its work." in html
    assert "Price set in Stripe" in html
    assert "Checkout is not open yet" not in html
    assert "Log in with passkey" in html
    assert 'href="/roadmap"' in html

    roadmap = roadmap_snapshot(billing_ready=False)
    items = [item for group in roadmap["groups"] for item in group["items"]]
    assert next(item for item in items if item["key"] == "billing")["status"] == "ready"
    assert next(item for item in items if item["key"] == "biotech")["decision"] == "copy"
    assert next(item for item in items if item["key"] == "options")["decision"] == "cut"

    roadmap_response = roadmap_page(page_request("/roadmap"), None)
    roadmap_html = roadmap_response.body.decode()
    assert "Focused on the decision loop." in roadmap_html
    assert "Not building" in roadmap_html
    assert "Options flow and dealer analytics" in roadmap_html


def test_checkout_and_portal_use_stripe_as_the_source_of_truth(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_runner")
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_pro")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_runner")
    captured: dict[str, Any] = {}

    def fake_price(price_id: str, **kwargs: Any) -> dict[str, Any]:
        captured["price_expand"] = kwargs["expand"]
        return {
            "id": price_id,
            "unit_amount": 1900,
            "currency": "cad",
            "recurring": {"interval": "month", "interval_count": 1},
            "product": {"name": "Runner Watch Pro"},
        }

    def fake_checkout(**kwargs: Any) -> dict[str, str]:
        captured["checkout"] = kwargs
        return {"url": "https://checkout.stripe.test/session"}

    def fake_portal(**kwargs: Any) -> dict[str, str]:
        captured["portal"] = kwargs
        return {"url": "https://billing.stripe.test/session"}

    monkeypatch.setattr(billing.stripe.Price, "retrieve", fake_price)
    monkeypatch.setattr(billing.stripe.checkout.Session, "create", fake_checkout)
    monkeypatch.setattr(billing.stripe.billing_portal.Session, "create", fake_portal)

    summary = price_summary()
    checkout_url = create_checkout_session({"id": "user-one"}, "https://stonks.test")
    portal_url = create_portal_session(
        {"id": "user-one", "stripe_customer_id": "cus_one"},
        "https://stonks.test",
    )

    assert summary["amount"] == "CA$19"
    assert summary["interval"] == "per month"
    assert captured["price_expand"] == ["product"]
    assert checkout_url == "https://checkout.stripe.test/session"
    assert captured["checkout"]["mode"] == "subscription"
    assert captured["checkout"]["line_items"] == [{"price": "price_pro", "quantity": 1}]
    assert captured["checkout"]["subscription_data"]["metadata"] == {
        "runner_user_id": "user-one"
    }
    assert portal_url == "https://billing.stripe.test/session"
    assert captured["portal"]["customer"] == "cus_one"


def test_signed_subscription_events_control_the_local_entitlement(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "billing.db")
    init_db()
    created_at = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("billing-user", "billing_user", "Billing User", "active", created_at),
        )

    active_event = {
        "id": "evt_active",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_one",
                "customer": "cus_one",
                "status": "active",
                "metadata": {"runner_user_id": "billing-user"},
                "cancel_at_period_end": False,
                "items": {
                    "data": [
                        {
                            "price": {"id": "price_pro"},
                            "current_period_end": 1_800_000_000,
                        }
                    ]
                },
            }
        },
    }

    result = process_webhook_event(active_event)
    duplicate = process_webhook_event(active_event)

    with connection() as database:
        user = dict(database.execute("SELECT * FROM users WHERE id='billing-user'").fetchone())
        event_count = database.execute("SELECT COUNT(*) FROM stripe_webhook_events").fetchone()[0]
    assert result == {
        "handled": True,
        "duplicate": False,
        "type": "customer.subscription.created",
    }
    assert duplicate["duplicate"] is True
    assert event_count == 1
    assert user["plan"] == "subscriber"
    assert user["stripe_subscription_status"] == "active"
    assert user["stripe_subscription_price_id"] == "price_pro"
    assert billing_account(user)["has_access"] is True

    canceled_event = {
        "id": "evt_canceled",
        "type": "customer.subscription.deleted",
        "data": {"object": {**active_event["data"]["object"], "status": "canceled"}},
    }
    process_webhook_event(canceled_event)
    with connection() as database:
        canceled = dict(
            database.execute("SELECT * FROM users WHERE id='billing-user'").fetchone()
        )
    assert canceled["plan"] == "free"
    assert canceled["stripe_subscription_status"] == "canceled"
    assert billing_account(canceled)["has_access"] is False


def test_checkout_completion_links_customer_without_granting_pro(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "checkout.db")
    init_db()
    created_at = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("checkout-user", "checkout_user", "Checkout User", "active", created_at),
        )

    process_webhook_event(
        {
            "id": "evt_checkout",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": "checkout-user",
                    "customer": "cus_checkout",
                    "subscription": "sub_checkout",
                    "metadata": {"runner_user_id": "checkout-user"},
                }
            },
        }
    )

    with connection() as database:
        user = dict(database.execute("SELECT * FROM users WHERE id='checkout-user'").fetchone())
    assert user["plan"] == "free"
    assert user["stripe_subscription_status"] == "pending"
    assert user["stripe_customer_id"] == "cus_checkout"


def test_private_research_requires_pro_and_stays_owner_only(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_main, "require_origin", lambda request: None)
    monkeypatch.setattr(
        web_main,
        "require_user",
        lambda session: {"id": "free-user", "plan": "free"},
    )

    with pytest.raises(HTTPException) as free_error:
        asyncio.run(
            web_main.commission_research_api(
                "ONE",
                post_request("/api/research/ONE"),
                "free-session",
            )
        )
    assert free_error.value.status_code == 403

    monkeypatch.setattr(
        web_main,
        "get_commission",
        lambda public_id: {"public_id": public_id, "user_id": "owner-user"},
    )
    monkeypatch.setattr(
        web_main,
        "current_user",
        lambda session: {"id": "different-user", "plan": "subscriber"},
    )

    with pytest.raises(HTTPException) as owner_error:
        web_main.research_report_page(
            "private-report",
            page_request("/research/private-report"),
            "other-session",
        )
    assert owner_error.value.status_code == 404
