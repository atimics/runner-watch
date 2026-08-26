from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import stripe

from runner_web.caller_ids import (
    CALLER_ID_CLAIM_PRICE_CENTS,
    claim_caller_id_with_database,
)
from runner_web.db import connection

ACCESS_STATUSES = {"active", "trialing"}
CUSTOMER_ACTION_STATUSES = {"past_due", "unpaid", "paused"}
STRIPE_BILLING_ENABLED = False


@dataclass(frozen=True, slots=True)
class BillingConfig:
    secret_key: str
    pro_price_id: str
    webhook_secret: str
    caller_id_price_id: str = ""
    caller_id_currency: str = "cad"

    @property
    def checkout_ready(self) -> bool:
        return bool(
            STRIPE_BILLING_ENABLED
            and self.secret_key
            and self.pro_price_id
            and self.webhook_secret
        )

    @property
    def portal_ready(self) -> bool:
        return bool(STRIPE_BILLING_ENABLED and self.secret_key)

    @property
    def caller_id_ready(self) -> bool:
        return bool(self.secret_key and self.webhook_secret)


def billing_config() -> BillingConfig:
    return BillingConfig(
        secret_key=os.getenv("STRIPE_SECRET_KEY", "").strip(),
        pro_price_id=os.getenv("STRIPE_PRO_PRICE_ID", "").strip(),
        webhook_secret=os.getenv("STRIPE_WEBHOOK_SECRET", "").strip(),
        caller_id_price_id=os.getenv("STRIPE_CALLER_ID_PRICE_ID", "").strip(),
        caller_id_currency=os.getenv("CALLER_ID_CURRENCY", "cad").strip().lower() or "cad",
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


def _format_amount(unit_amount: Any, currency: Any) -> str | None:
    try:
        amount = int(unit_amount)
    except (TypeError, ValueError):
        return None
    code = str(currency or "").lower()
    if code in {"jpy", "krw"}:
        major = f"{amount:,}"
    else:
        major = f"{amount / 100:,.2f}".removesuffix(".00")
    prefix = {"cad": "CA$", "usd": "$", "eur": "€", "gbp": "£"}.get(code)
    return f"{prefix}{major}" if prefix else f"{major} {code.upper()}".strip()


def price_summary(config: BillingConfig | None = None) -> dict[str, Any]:
    config = config or billing_config()
    fallback = {
        "id": config.pro_price_id or None,
        "name": "Runner Watch Pro",
        "amount": None,
        "interval": None,
        "available": config.checkout_ready,
    }
    if not config.secret_key or not config.pro_price_id:
        return fallback
    stripe.api_key = config.secret_key
    try:
        price = stripe.Price.retrieve(config.pro_price_id, expand=["product"])
    except stripe.StripeError:
        return fallback
    product = _get(price, "product", {})
    recurring = _get(price, "recurring", {}) or {}
    interval = _get(recurring, "interval")
    interval_count = int(_get(recurring, "interval_count", 1) or 1)
    interval_label = None
    if interval:
        interval_label = (
            f"every {interval_count} {interval}s" if interval_count > 1 else f"per {interval}"
        )
    return {
        "id": str(_get(price, "id") or config.pro_price_id),
        "name": str(_get(product, "name") or "Runner Watch Pro"),
        "amount": _format_amount(_get(price, "unit_amount"), _get(price, "currency")),
        "interval": interval_label,
        "available": config.checkout_ready,
    }


def billing_account(user: dict[str, Any] | None) -> dict[str, Any]:
    status = str((user or {}).get("stripe_subscription_status") or "none")
    plan = str((user or {}).get("plan") or "free")
    return {
        "plan": plan,
        "plan_label": "Pro" if plan == "subscriber" else "Free",
        "status": status,
        "status_label": {
            "active": "Active",
            "trialing": "Trial",
            "past_due": "Payment needs attention",
            "unpaid": "Payment needs attention",
            "paused": "Paused",
            "canceled": "Canceled",
            "incomplete": "Checkout incomplete",
            "incomplete_expired": "Checkout expired",
            "pending": "Waiting for Stripe",
        }.get(status, "Free"),
        "has_access": status in ACCESS_STATUSES or plan == "subscriber",
        "needs_action": status in CUSTOMER_ACTION_STATUSES,
        "customer_id": (user or {}).get("stripe_customer_id"),
        "current_period_end": (user or {}).get("stripe_current_period_end"),
        "cancel_at_period_end": bool((user or {}).get("stripe_cancel_at_period_end")),
    }


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


def create_portal_session(user: dict[str, Any], app_origin: str) -> str:
    config = billing_config()
    customer_id = str(user.get("stripe_customer_id") or "").strip()
    if not config.portal_ready or not customer_id:
        raise RuntimeError("Stripe customer portal is not available")
    stripe.api_key = config.secret_key
    session = stripe.billing_portal.Session.create(
        customer=customer_id,
        return_url=f"{app_origin}/billing",
    )
    url = str(_get(session, "url") or "")
    if not url:
        raise RuntimeError("Stripe did not return a portal URL")
    return url


def delete_customer(user: dict[str, Any]) -> bool:
    """Delete the linked Stripe customer and cancel its active subscriptions."""

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


def caller_id_price_label(config: BillingConfig | None = None) -> str:
    config = config or billing_config()
    return _format_amount(CALLER_ID_CLAIM_PRICE_CENTS, config.caller_id_currency) or ""


def create_caller_id_checkout_session(user: dict[str, Any], app_origin: str) -> str:
    config = billing_config()
    if not config.caller_id_ready:
        raise RuntimeError("Stripe caller-ID checkout is not configured")
    stripe.api_key = config.secret_key
    line_item: dict[str, Any]
    if config.caller_id_price_id:
        line_item = {"price": config.caller_id_price_id, "quantity": 1}
    else:
        line_item = {
            "price_data": {
                "currency": config.caller_id_currency,
                "unit_amount": CALLER_ID_CLAIM_PRICE_CENTS,
                "product_data": {"name": "Runner Watch caller ID"},
            },
            "quantity": 1,
        }
    parameters: dict[str, Any] = {
        "mode": "payment",
        "line_items": [line_item],
        "client_reference_id": str(user["id"]),
        "metadata": {
            "runner_user_id": str(user["id"]),
            "purpose": "caller_identity",
        },
        "success_url": f"{app_origin}/privacy?caller=claimed",
        "cancel_url": f"{app_origin}/privacy?caller=canceled",
    }
    customer_id = str(user.get("stripe_customer_id") or "").strip()
    if customer_id:
        parameters["customer"] = customer_id
    else:
        parameters["customer_creation"] = "always"
    session = stripe.checkout.Session.create(**parameters)
    url = str(_get(session, "url") or "")
    if not url:
        raise RuntimeError("Stripe did not return a Checkout URL")
    return url


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


def _apply_caller_id_checkout(database: Any, session: Any) -> bool:
    metadata = _get(session, "metadata", {}) or {}
    user_id = str(
        _get(metadata, "runner_user_id") or _get(session, "client_reference_id") or ""
    ).strip()
    session_id = str(_get(session, "id") or "").strip()
    payment_status = str(_get(session, "payment_status") or "").strip()
    if not user_id or not session_id or payment_status not in {"paid", "no_payment_required"}:
        return False
    customer_id = str(_get(session, "customer") or "").strip() or None
    if customer_id:
        database.execute(
            "UPDATE users SET stripe_customer_id=COALESCE(stripe_customer_id,?) WHERE id=?",
            (customer_id, user_id),
        )
    claim_caller_id_with_database(
        database,
        user_id,
        payment_reference=f"stripe:{session_id}",
    )
    return True


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
        if event_type == "checkout.session.completed":
            metadata = _get(data_object, "metadata", {}) or {}
            if _get(metadata, "purpose") == "caller_identity":
                handled = _apply_caller_id_checkout(database, data_object)
            elif STRIPE_BILLING_ENABLED:
                handled = _apply_checkout(database, data_object)
        elif STRIPE_BILLING_ENABLED and event_type in {
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        }:
            handled = _apply_subscription(database, data_object, event_type)
    return {"handled": handled, "duplicate": False, "type": event_type}
