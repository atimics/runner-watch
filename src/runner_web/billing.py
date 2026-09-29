from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import stripe

from runner_web.db import connection

ACCESS_STATUSES = {"active", "trialing"}
CUSTOMER_ACTION_STATUSES = {"past_due", "unpaid", "paused"}
STRIPE_BILLING_ENABLED = False


@dataclass(frozen=True, slots=True)
class BillingConfig:
    secret_key: str
    pro_price_id: str
    webhook_secret: str

    @property
    def checkout_ready(self) -> bool:
        return bool(
            STRIPE_BILLING_ENABLED and self.secret_key and self.pro_price_id and self.webhook_secret
        )

    @property
    def portal_ready(self) -> bool:
        return bool(STRIPE_BILLING_ENABLED and self.secret_key)


def billing_config() -> BillingConfig:
    return BillingConfig(
        secret_key=os.getenv("STRIPE_SECRET_KEY", "").strip(),
        pro_price_id=os.getenv("STRIPE_PRO_PRICE_ID", "").strip(),
        webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET", "").strip(),
    )


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _iso_from_timestamp(value: Any) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _price_id(subscription: Any) -> str | None:
    items = _get(_get(subscription, "items", {}), "data", []) or []
    if not items:
        return None
    price = _get(items[0], "price", {})
    value = _get(price, "id")
    return str(value) if value else None


def _period_end(subscription: Any) -> str | None:
    value = _get(subscription, "current_period_end")
    if value is None:
        items = _get(_get(subscription, "items", {}), "data", []) or []
        value = _get(items[0], "current_period_end") if items else None
    return _iso_from_timestamp(value)


def create_checkout_session(user: dict[str, Any], app_origin: str) -> str:
    config = billing_config()
    if not config.checkout_ready:
        raise RuntimeError("Stripe Checkout is not configured")
    stripe.api_key = config.secret_key
    parameters: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": config.pro_price_id, "quantity": 1}],
        "client_reference_id": str(user["id"]),
        "metadata": {"runner_user_id": str(user["id"])},
        "subscription_data": {"metadata": {"runner_user_id": str(user["id"])}},
        "success_url": f"{app_origin}/billing?checkout=success",
        "cancel_url": f"{app_origin}/billing?checkout=canceled",
        "allow_promotion_codes": True,
    }
    customer_id = str(user.get("stripe_customer_id") or "").strip()
    if customer_id:
        parameters["customer"] = customer_id
    session = stripe.checkout.Session.create(**parameters)
    url = str(_get(session, "url") or "")
    if not url:
        raise RuntimeError("Stripe did not return a Checkout URL")
    return url


def delete_customer(user: dict[str, Any]) -> bool:

    customer_id = str(user.get("stripe_customer_id") or "").strip()
    if not customer_id:
        return False
    config = billing_config()
    if not config.secret_key:
        raise RuntimeError("Stripe is not configured")
    stripe.api_key = config.secret_key
    try:
        deleted = stripe.Customer.delete(customer_id)
    except stripe.InvalidRequestError as exc:
        if getattr(exc, "code", None) == "resource_missing":
            return True
        raise
    if not bool(_get(deleted, "deleted", False)):
        raise RuntimeError("Stripe did not confirm customer deletion")
    return True


def construct_webhook_event(payload: bytes, signature: str) -> Any:
    config = billing_config()
    if not config.webhook_secret:
        raise RuntimeError("Stripe webhook is not configured")
    return stripe.Webhook.construct_event(payload, signature, config.webhook_secret)


def _user_for_billing_object(database: Any, value: Any) -> str | None:
    metadata = _get(value, "metadata", {}) or {}
    user_id = str(_get(metadata, "runner_user_id") or "").strip()
    if user_id:
        row = database.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if row:
            return str(row["id"])
    subscription_id = str(_get(value, "id") or "").strip()
    customer_id = str(_get(value, "customer") or "").strip()
    row = database.execute(
        """
        SELECT id FROM users
        WHERE (?<>'' AND stripe_subscription_id=?)
           OR (?<>'' AND stripe_customer_id=?)
        LIMIT 1
        """,
        (subscription_id, subscription_id, customer_id, customer_id),
    ).fetchone()
    return str(row["id"]) if row else None


def _apply_checkout(database: Any, session: Any) -> bool:
    metadata = _get(session, "metadata", {}) or {}
    user_id = str(
        _get(metadata, "runner_user_id") or _get(session, "client_reference_id") or ""
    ).strip()
    if not user_id:
        return False
    customer_id = str(_get(session, "customer") or "").strip() or None
    subscription_id = str(_get(session, "subscription") or "").strip() or None
    updated = database.execute(
        """
        UPDATE users SET
            stripe_customer_id=COALESCE(?,stripe_customer_id),
            stripe_subscription_id=COALESCE(?,stripe_subscription_id),
            stripe_subscription_status=CASE
                WHEN stripe_subscription_status='none' THEN 'pending'
                ELSE stripe_subscription_status
            END,
            billing_updated_at=?
        WHERE id=?
        """,
        (customer_id, subscription_id, datetime.now(UTC).isoformat(), user_id),
    )
    return updated.rowcount > 0


def _apply_subscription(database: Any, subscription: Any, event_type: str) -> bool:
    user_id = _user_for_billing_object(database, subscription)
    if not user_id:
        return False
    status = str(_get(subscription, "status") or "")
    if event_type == "customer.subscription.deleted":
        status = "canceled"
    customer_id = str(_get(subscription, "customer") or "").strip() or None
    subscription_id = str(_get(subscription, "id") or "").strip() or None
    plan = "subscriber" if status in ACCESS_STATUSES else "free"
    database.execute(
        """
        UPDATE users SET
            plan=?,stripe_customer_id=COALESCE(?,stripe_customer_id),
            stripe_subscription_id=COALESCE(?,stripe_subscription_id),
            stripe_subscription_status=?,stripe_subscription_price_id=?,
            stripe_current_period_end=?,stripe_cancel_at_period_end=?,billing_updated_at=?
        WHERE id=?
        """,
        (
            plan,
            customer_id,
            subscription_id,
            status or "none",
            _price_id(subscription),
            _period_end(subscription),
            int(bool(_get(subscription, "cancel_at_period_end", False))),
            datetime.now(UTC).isoformat(),
            user_id,
        ),
    )
    return True


def process_webhook_event(event: Any) -> dict[str, Any]:
    event_id = str(_get(event, "id") or "").strip()
    event_type = str(_get(event, "type") or "").strip()
    data_object = _get(_get(event, "data", {}), "object", {})
    if not event_id or not event_type:
        raise ValueError("Stripe event is missing its id or type")
    with connection() as database:
        inserted = database.execute(
            """
            INSERT INTO stripe_webhook_events(event_id,event_type,received_at)
            VALUES(?,?,?) ON CONFLICT DO NOTHING
            """,
            (event_id, event_type, datetime.now(UTC).isoformat()),
        )
        if inserted.rowcount == 0:
            return {"handled": False, "duplicate": True, "type": event_type}
        handled = False
        if event_type == "checkout.session.completed" and STRIPE_BILLING_ENABLED:
            handled = _apply_checkout(database, data_object)
        elif STRIPE_BILLING_ENABLED and event_type in {
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        }:
            handled = _apply_subscription(database, data_object, event_type)
    return {"handled": handled, "duplicate": False, "type": event_type}
