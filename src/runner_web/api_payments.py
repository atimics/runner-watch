"""x402 v2 USDC top-ups. Settlement receipts are unique across accounts."""

from __future__ import annotations

import base64
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from x402.http import HTTPFacilitatorClientSync
from x402.schemas import PaymentPayload, PaymentRequirements

from runner_web import api_credits
from runner_web.db import connection

NETWORKS = {
    "production": {"base": "eip155:8453", "solana": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"},
    "test": {"base": "eip155:84532", "solana": "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"},
}


def configuration() -> dict[str, Any]:
    mode = os.getenv("X402_MODE", "test")
    if mode not in NETWORKS:
        raise HTTPException(503, "Choose a supported payment environment.")
    prefix = "X402_" if mode == "production" else "X402_TEST_"
    receivers = {
        chain: os.getenv(prefix + chain.upper() + "_PAY_TO", "").strip()
        for chain in ("base", "solana")
    }
    if receivers["base"]:
        from eth_utils import is_address

        if not is_address(receivers["base"]) or int(receivers["base"], 16) == 0:
            raise HTTPException(503, "Set a valid Base receiving address in Fly secrets.")
    if receivers["solana"]:
        from solders.pubkey import Pubkey

        try:
            public = Pubkey.from_string(receivers["solana"])
            if public == Pubkey.default() or not public.is_on_curve():
                raise ValueError("Use a wallet address")
        except ValueError as exc:
            raise HTTPException(503, "Set a valid Solana wallet address in Fly secrets.") from exc
    ready = bool(os.getenv("X402_PAYMENTS_ENABLED", "0") == "1" and any(receivers.values()))
    if mode == "production":
        ready = ready and bool(os.getenv("CDP_API_KEY_ID") and os.getenv("CDP_API_KEY_SECRET"))
    return {"mode": mode, "receivers": receivers, "enabled": ready}


def facilitator() -> HTTPFacilitatorClientSync:
    config = configuration()
    if not config["enabled"]:
        raise HTTPException(503, "Payment setup is pending. Free API credits are available.")
    if config["mode"] == "test":
        return HTTPFacilitatorClientSync({"url": "https://x402.org/facilitator", "timeout": 30})
    from cdp.x402 import create_facilitator_config

    return HTTPFacilitatorClientSync({**create_facilitator_config(), "timeout": 30})


def requirements(dollars: int) -> list[dict[str, Any]]:
    from x402.mechanisms.evm.exact.server import ExactEvmScheme
    from x402.mechanisms.svm.exact.server import ExactSvmScheme

    config = configuration()
    with facilitator() as service:
        supported = service.get_supported()
    accepts = []
    for chain, receiver in config["receivers"].items():
        if not receiver:
            continue
        network = NETWORKS[config["mode"]][chain]
        kind = next(
            (
                item
                for item in supported.kinds
                if item.x402_version == 2 and item.network == network and item.scheme == "exact"
            ),
            None,
        )
        if kind is None:
            continue
        scheme = ExactEvmScheme() if chain == "base" else ExactSvmScheme()
        price = scheme.parse_price(f"${dollars}.00", network)
        requirement = PaymentRequirements(
            scheme="exact",
            network=network,
            amount=price.amount,
            asset=price.asset,
            pay_to=receiver,
            max_timeout_seconds=120,
            extra=price.extra or {},
        )
        requirement = scheme.enhance_payment_requirements(requirement, kind, [])
        if chain == "solana" and not requirement.extra.get("feePayer"):
            continue
        accepts.append(requirement.model_dump(by_alias=True, exclude_none=True))
    if not accepts:
        raise HTTPException(503, "The payment service is preparing the configured networks.")
    return accepts


def order(user_id: str, dollars: int, request_key: str, origin: str) -> dict[str, Any]:
    with connection() as db:
        previous = db.execute(
            "SELECT * FROM api_topups WHERE user_id=? AND request_key=?", (user_id, request_key)
        ).fetchone()
    if previous:
        if previous["dollars"] != dollars:
            raise HTTPException(409, "Use a new Idempotency-Key for a different pack.")
        return dict(previous)
    if dollars not in api_credits.PACKS:
        raise HTTPException(422, "Choose a $1, $5, or $20 credit pack.")
    if not origin.startswith("https://"):
        raise HTTPException(503, "Configure the public HTTPS API origin.")
    with connection() as db:
        recent = db.execute(
            "SELECT COUNT(*) AS n FROM api_topups WHERE user_id=? AND created_at>?",
            (user_id, (datetime.now(UTC) - timedelta(hours=1)).isoformat()),
        ).fetchone()
        if recent["n"] >= 10:
            raise HTTPException(
                429,
                "Try your existing top-up order or return in an hour.",
                headers={"Retry-After": "3600"},
            )
    accepts = requirements(dollars)
    identifier = secrets.token_hex(16)
    now = api_credits.stamp()
    resource = origin.rstrip("/") + "/api/v1/topups/" + identifier
    with connection() as db:
        api_credits.account(db, user_id)
        # The account lock also resolves simultaneous order creation with the same key.
        db.execute(
            "INSERT INTO api_topups(id,user_id,request_key,dollars,credits,requirements,resource,"
            "status,lease_until,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'open',?,?,?) "
            "ON CONFLICT(user_id,request_key) DO NOTHING",
            (
                identifier,
                user_id,
                request_key,
                dollars,
                api_credits.PACKS[dollars],
                json.dumps(accepts),
                resource,
                now,
                now,
                now,
            ),
        )
        row = dict(
            db.execute(
                "SELECT * FROM api_topups WHERE user_id=? AND request_key=?", (user_id, request_key)
            ).fetchone()
        )
        if row["dollars"] != dollars:
            raise HTTPException(409, "Use a new Idempotency-Key for a different pack.")
    return row


def get_order(identifier: str, user_id: str) -> dict[str, Any]:
    with connection() as db:
        row = db.execute(
            "SELECT * FROM api_topups WHERE id=? AND user_id=?", (identifier, user_id)
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Top-up order unknown.")
    return dict(row)


def challenge(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "x402Version": 2,
        "resource": {
            "url": row["resource"],
            "description": f"{row['credits']} Runner Watch API credits",
            "mimeType": "application/json",
        },
        "accepts": json.loads(row["requirements"]),
    }


def encode(value: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(value, separators=(",", ":")).encode()).decode()


def proof_id(payload: PaymentPayload) -> str:
    if payload.accepted.network.startswith("eip155:"):
        authorization = payload.payload.get("authorization") or {}
        identity = [
            payload.accepted.network,
            str(authorization.get("from", "")).lower(),
            str(authorization.get("nonce", "")).lower(),
        ]
        if not all(identity):
            raise ValueError("Use an exact USDC authorization")
    else:
        raw = base64.b64decode(payload.payload["transaction"], validate=True)
        identity = [payload.accepted.network, base64.b64encode(raw).decode()]
    return api_credits.digest(json.dumps(identity))


def settle(identifier: str, user_id: str, signature: str) -> dict[str, Any]:
    if not configuration()["enabled"]:
        raise HTTPException(503, "Payment setup is pending.")
    try:
        if len(signature) > 16384:
            raise ValueError("Payment header too large")
        payload = PaymentPayload.model_validate_json(base64.b64decode(signature, validate=True))
        proof = proof_id(payload)
    except Exception as exc:
        raise HTTPException(400, "Use a valid x402 v2 PAYMENT-SIGNATURE header.") from exc
    now = datetime.now(UTC)
    with connection() as db:
        api_credits.account(db, user_id)
        found = db.execute(
            "SELECT * FROM api_topups WHERE id=? AND user_id=?", (identifier, user_id)
        ).fetchone()
        if found is None:
            raise HTTPException(404, "Top-up order unknown.")
        row = dict(found)
        if row["status"] == "complete":
            return row
        accepted = payload.accepted.model_dump(by_alias=True, exclude_none=True)
        if payload.x402_version != 2 or accepted not in json.loads(row["requirements"]):
            raise HTTPException(400, "Use the payment terms from this order.")
        if payload.resource is None or payload.resource.url != row["resource"]:
            raise HTTPException(400, "Use the resource URL from this order.")
        if row["proof_hash"] and row["proof_hash"] != proof:
            raise HTTPException(409, "Retry this order with its original payment.")
        if row["status"] == "open" and now - datetime.fromisoformat(row["created_at"]) > timedelta(
            minutes=10
        ):
            raise HTTPException(410, "Create a new top-up order for current payment terms.")
        if row["status"] in {"verifying", "settling"} and row["lease_until"] > now.isoformat():
            raise HTTPException(
                409, "Payment confirmation is in progress.", headers={"Retry-After": "45"}
            )
        db.execute(
            "INSERT INTO api_payment_claims(id,order_id) VALUES(?,?) ON CONFLICT DO NOTHING",
            ("proof:" + proof, identifier),
        )
        claim = db.execute(
            "SELECT order_id FROM api_payment_claims WHERE id=?", ("proof:" + proof,)
        ).fetchone()
        if claim["order_id"] != identifier:
            raise HTTPException(409, "This payment belongs to an existing order.")
        retry_settle = row["status"] in {"settling", "pending"}
        db.execute(
            "UPDATE api_topups SET status=?,proof_hash=?,proof_json=?,lease_until=?,updated_at=? "
            "WHERE id=?",
            (
                "settling" if retry_settle else "verifying",
                proof,
                payload.model_dump_json(by_alias=True, exclude_none=True),
                (now + timedelta(seconds=90)).isoformat(),
                now.isoformat(),
                identifier,
            ),
        )
    requirement = PaymentRequirements.model_validate(accepted)
    try:
        with facilitator() as service:
            if not retry_settle:
                verified = service.verify(payload, requirement)
                if not verified.is_valid:
                    with connection() as db:
                        db.execute(
                            "UPDATE api_topups SET status='rejected',updated_at=? WHERE id=?",
                            (api_credits.stamp(), identifier),
                        )
                    raise HTTPException(
                        402, "The payment service declined this authorization. Create a new order."
                    )
                with connection() as db:
                    db.execute(
                        "UPDATE api_topups SET status='settling',updated_at=? WHERE id=?",
                        (api_credits.stamp(), identifier),
                    )
            settled = service.settle(payload, requirement)
            if (
                not settled.success
                or not settled.transaction
                or settled.network != requirement.network
            ):
                raise ValueError("Settlement is awaiting confirmation")
    except HTTPException:
        raise
    except Exception as exc:
        with connection() as db:
            db.execute(
                "UPDATE api_topups SET status='pending',updated_at=? WHERE id=?",
                (api_credits.stamp(), identifier),
            )
        raise HTTPException(
            503,
            {
                "message": (
                    "Payment confirmation is pending. "
                    "Keep this order ID and retry the same payment."
                ),
                "order_id": identifier,
            },
            headers={"Retry-After": "90"},
        ) from exc
    receipt = settled.model_dump(by_alias=True, exclude_none=True)
    with connection() as db:
        api_credits.account(db, user_id)
        claim_id = "settled:" + api_credits.digest(settled.network + ":" + settled.transaction)
        db.execute(
            "INSERT INTO api_payment_claims(id,order_id) VALUES(?,?) ON CONFLICT DO NOTHING",
            (claim_id, identifier),
        )
        claimed = db.execute(
            "SELECT order_id FROM api_payment_claims WHERE id=?", (claim_id,)
        ).fetchone()
        if claimed["order_id"] != identifier:
            raise HTTPException(409, "This settlement belongs to an existing order.")
        changed = db.execute(
            "UPDATE api_topups SET status='complete',network=?,transaction_id=?,settlement_json=?,"
            "proof_json=NULL,updated_at=? WHERE id=? AND user_id=? AND status<>'complete'",
            (
                settled.network,
                settled.transaction,
                json.dumps(receipt),
                api_credits.stamp(),
                identifier,
                user_id,
            ),
        )
        if changed.rowcount:
            db.execute(
                "UPDATE api_accounts SET paid_credits=paid_credits+? WHERE user_id=?",
                (row["credits"], user_id),
            )
    return get_order(identifier, user_id)


def main() -> None:
    config = configuration()
    accepts = requirements(1)
    print(
        json.dumps(
            {
                "environment": config["mode"],
                "enabled": config["enabled"],
                "networks": [item["network"] for item in accepts],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
