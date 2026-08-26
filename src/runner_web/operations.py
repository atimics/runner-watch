from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from runner_web.ai_kol import FLASH
from runner_web.billing import billing_config
from runner_web.db import connection
from runner_web.ingestion import ingestion_status
from runner_web.product_policy import OPERATIONS, policy_manifest
from runner_web.ranker import ranker_status
from runner_web.source_catalog import DEFAULT_SOURCE_POLICIES

WORKER_HEARTBEAT_KEY = "worker_process_heartbeat"
router = APIRouter()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def worker_health(
    states: dict[str, dict[str, Any]],
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    checked_at = checked_at or datetime.now(UTC)
    heartbeat = states.get(WORKER_HEARTBEAT_KEY)
    heartbeat_at = _time(heartbeat.get("updated_at")) if heartbeat else None
    age_seconds = (
        max(0.0, (checked_at - heartbeat_at).total_seconds())
        if heartbeat_at is not None
        else None
    )
    fresh = bool(
        age_seconds is not None
        and age_seconds <= OPERATIONS.worker_heartbeat_max_age_seconds
    )
    return {
        "status": "ok" if fresh else "stale",
        "last_heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "maximum_age_seconds": OPERATIONS.worker_heartbeat_max_age_seconds,
        "detail": heartbeat.get("value") if heartbeat else None,
    }


def health_status(*, checked_at: datetime | None = None) -> dict[str, Any]:
    checked_at = checked_at or datetime.now(UTC)
    with connection() as database:
        database.execute("SELECT 1").fetchone()
        states = {
            str(row["key"]): {
                "value": row["value"],
                "updated_at": row["updated_at"],
            }
            for row in database.execute(
                "SELECT key,value,updated_at FROM worker_state"
            ).fetchall()
        }
        latest_scan = database.execute(
            "SELECT MAX(captured_at) FROM scan_runs"
        ).fetchone()[0]
    workers = worker_health(states, checked_at=checked_at)
    return {
        "status": "ok" if workers["status"] == "ok" else "degraded",
        "checked_at": checked_at.isoformat(),
        "database": "ok",
        "worker": workers,
        "latest_scan_at": latest_scan,
        "edgar_updated_at": states.get("edgar_last_refresh", {}).get("updated_at"),
        "scan_error": states.get("background_scan_last_error", {}).get("value") or None,
    }


def runtime_capabilities(
    worker_tasks: list[Any] | None = None,
) -> dict[str, Any]:
    """Describe the deployment from live state and the shared product policy."""

    ingestion = ingestion_status()
    source_rows = ingestion["sources"]
    sources = {
        f"{row['source']}:{row['feed']}": {
            "title": row["title"],
            "enabled": bool(row["enabled"]),
            "state": row["health"],
            "last_success_at": row.get("last_success_at"),
            "age_seconds": row.get("age_seconds"),
            "review_status": row["review_status"],
        }
        for row in source_rows
    }

    def feature(*keys: str) -> dict[str, Any]:
        matched = [sources[key] for key in keys if key in sources]
        live = [row for row in matched if row["enabled"] and row["state"] == "healthy"]
        enabled = [row for row in matched if row["enabled"]]
        state = (
            "healthy"
            if live
            else "degraded"
            if enabled
            else "disabled"
            if matched
            else "unavailable"
        )
        return {"state": state, "sources": list(keys)}

    ranker = ranker_status()
    model = ranker.get("model")
    tasks = worker_tasks or []
    failed_workers = sum(task.done() for task in tasks)
    source_problems = sum(
        row["enabled"] and row["health"] in {"failed", "stale"}
        for row in source_rows
    )
    deployment_health = health_status()
    manifest = policy_manifest(DEFAULT_SOURCE_POLICIES)
    stripe_billing = billing_config()
    policy_warnings = manifest["source_policy_warnings"]
    policy_blockers = [
        warning for warning in policy_warnings if warning["severity"] == "blocking"
    ]
    evidence_gate = manifest["evidence_gate"]
    base_rates = manifest["market_base_rates"]
    return {
        "checked_at": ingestion["checked_at"],
        "status": (
            "degraded"
            if (
                failed_workers
                or source_problems
                or policy_blockers
                or deployment_health["worker"]["status"] != "ok"
            )
            else "ok"
        ),
        "policy_version": manifest["version"],
        "policy_warnings": policy_warnings,
        "features": {
            "sec_filings": feature("sec:current_filings"),
            "issuer_facts": feature("sec:company_facts"),
            "market_bars": feature("yahoo:market_bars"),
            "news": feature("yahoo:news_search", "gdelt:news_search"),
            "public_social": feature(
                "apewisdom:reddit_trends", "bluesky:social_search"
            ),
            "trading_halts": feature("nasdaq_trader:trade_halts"),
            "short_positioning": feature(
                "fintel:short_interest", "fintel:borrow_rate"
            ),
            "billing": {
                "state": "configured" if stripe_billing.checkout_ready else "unavailable",
                "provider": "stripe",
            },
        },
        "analysis": {
            "evidence_gate": evidence_gate,
            "market_base_rates": {
                "mode": "empirical_matched_sessions",
                **base_rates,
            },
            "ranker": {
                "state": "shadow" if model else "learning",
                "model_id": model.get("id") if model else None,
                "engine": ranker.get("engine"),
                "integer_only": ranker.get("integer_only"),
                "model_kind": ranker.get("model_kind"),
                "feature_schema_version": ranker["feature_schema_version"],
                "barrier_labeled": ranker["barrier_labeled"],
                "training_policy": manifest["ranker_training"],
            },
            "research": {
                "openai_available": bool(os.getenv("OPENAI_API_KEY", "")),
                "openrouter_available": bool(os.getenv("OPENROUTER_API_KEY", "")),
                "provider": FLASH.provider,
                "flash_model": FLASH.model,
                "mode": (
                    "one_shot_system_context"
                    if FLASH.provider == "openrouter"
                    else "verified_agent_pipeline"
                ),
                "promotion_policy": manifest["research_promotion"],
            },
        },
        "workers": {
            "running": sum(not task.done() for task in tasks),
            "failed": failed_workers,
            "process": deployment_health["worker"],
        },
        "source_summary": ingestion["summary"],
        "sources": sources,
    }


@router.get("/health")
def health_api() -> JSONResponse:
    payload = health_status()
    return JSONResponse(payload, status_code=200 if payload["status"] == "ok" else 503)


@router.get("/api/ranker/status")
def ranker_status_api() -> dict[str, Any]:
    return ranker_status()


@router.get("/api/ingestion/status")
def ingestion_status_api() -> dict[str, Any]:
    return ingestion_status()


@router.get("/api/capabilities")
def capabilities_api(request: Request) -> dict[str, Any]:
    return runtime_capabilities(getattr(request.app.state, "worker_tasks", []))
