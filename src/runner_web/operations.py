from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from runner_node.runtime import NODE_SERVICE
from runner_watch.source_catalog import DEFAULT_SOURCE_POLICIES
from runner_web.ai_kol import FLASH
from runner_web.db import MIGRATIONS, connection
from runner_web.flash_wallet import (
    COMMENT_COST,
    DAILY_CLAIM_AMOUNT,
    REPORT_COST,
    REPORT_EXCLUSIVE_HOURS,
)
from runner_web.ingestion import ingestion_status
from runner_web.product_policy import OPERATIONS, policy_manifest
from runner_web.ranker import ranker_status

WORKER_HEARTBEAT_KEY = "worker_process_heartbeat"
WORKER_HEARTBEAT_PREFIX = f"{WORKER_HEARTBEAT_KEY}:"
TRAINER_HEARTBEAT_KEY = "ranker_trainer_heartbeat"
WORKER_EXPECTED_INSTANCES = max(1, int(os.getenv("WORKER_EXPECTED_INSTANCES", "1")))
router = APIRouter()


def worker_heartbeat_key(instance_id: str) -> str:
    return f"{WORKER_HEARTBEAT_PREFIX}{instance_id}"


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _heartbeat_detail(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def worker_health(
    states: dict[str, dict[str, Any]],
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    checked_at = checked_at or datetime.now(UTC)
    heartbeats = [
        (key.removeprefix(WORKER_HEARTBEAT_PREFIX), value)
        for key, value in states.items()
        if key.startswith(WORKER_HEARTBEAT_PREFIX)
    ]
    if not heartbeats and WORKER_HEARTBEAT_KEY in states:
        heartbeats = [("legacy", states[WORKER_HEARTBEAT_KEY])]

    instances: list[dict[str, Any]] = []
    retirement_age = OPERATIONS.worker_heartbeat_retire_seconds
    for instance_id, heartbeat in heartbeats:
        heartbeat_at = _time(heartbeat.get("updated_at"))
        if heartbeat_at is None:
            continue
        age = max(0.0, (checked_at - heartbeat_at).total_seconds())
        if age > retirement_age:
            continue
        instance_detail = _heartbeat_detail(heartbeat.get("value"))
        reported_status = str(instance_detail.get("status", "unknown")).lower()
        fresh = age <= OPERATIONS.worker_heartbeat_max_age_seconds
        instances.append(
            {
                "instance_id": instance_id,
                **instance_detail,
                "status": (
                    "stale" if not fresh else "ok" if reported_status == "ok" else "degraded"
                ),
                "updated_at": heartbeat_at.isoformat(),
                "age_seconds": round(age, 1),
            }
        )

    newest = min(instances, key=lambda item: item["age_seconds"]) if instances else None
    fresh_instances = sum(instance["status"] != "stale" for instance in instances)
    if not instances or fresh_instances == 0:
        status = "stale"
    elif (
        fresh_instances >= WORKER_EXPECTED_INSTANCES
        and all(instance["status"] == "ok" for instance in instances)
    ):
        status = "ok"
    else:
        status = "degraded"
    detail = {
        "instances": instances,
        "instances_expected": WORKER_EXPECTED_INSTANCES,
        "instances_fresh": fresh_instances,
        "instances_tracked": len(instances),
        "workers_running": sum(int(instance.get("workers_running", 0)) for instance in instances),
        "workers_expected": sum(int(instance.get("workers_expected", 0)) for instance in instances),
        "failed_workers": [
            name for instance in instances for name in instance.get("failed_workers", [])
        ],
    }
    return {
        "status": status,
        "last_heartbeat_at": newest["updated_at"] if newest else None,
        "age_seconds": newest["age_seconds"] if newest else None,
        "maximum_age_seconds": OPERATIONS.worker_heartbeat_max_age_seconds,
        "detail": detail,
    }


def trainer_health(
    states: dict[str, dict[str, Any]],
    *,
    checked_at: datetime | None = None,
) -> dict[str, Any]:
    """Report whether the separate model trainer is alive and making progress."""

    checked_at = checked_at or datetime.now(UTC)
    heartbeat = states.get(TRAINER_HEARTBEAT_KEY)
    heartbeat_at = _time(heartbeat.get("updated_at")) if heartbeat else None
    detail = _heartbeat_detail(heartbeat.get("value")) if heartbeat else {}
    age = (
        max(0.0, (checked_at - heartbeat_at).total_seconds())
        if heartbeat_at is not None
        else None
    )
    fresh = age is not None and age <= OPERATIONS.worker_heartbeat_max_age_seconds
    reported = str(detail.get("status") or "unknown").lower()
    status = "ok" if fresh and reported == "ok" else "degraded" if fresh else "stale"
    return {
        "status": status,
        "last_heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
        "age_seconds": round(age, 1) if age is not None else None,
        "maximum_age_seconds": OPERATIONS.worker_heartbeat_max_age_seconds,
        "detail": detail,
    }


def readiness_status(*, checked_at: datetime | None = None) -> dict[str, Any]:
    """Report whether this web process can safely receive user traffic."""

    checked_at = checked_at or datetime.now(UTC)
    expected_schema_version = MIGRATIONS[-1].version if MIGRATIONS else 0
    try:
        with connection() as database:
            database.execute("SELECT 1").fetchone()
            row = database.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            schema_version = int(row[0] or 0)
    except Exception:
        return {
            "status": "unavailable",
            "checked_at": checked_at.isoformat(),
            "database": "unavailable",
            "schema_version": None,
            "minimum_schema_version": expected_schema_version,
        }

    schema_ready = schema_version >= expected_schema_version
    return {
        "status": "ok" if schema_ready else "unavailable",
        "checked_at": checked_at.isoformat(),
        "database": "ok",
        "schema_version": schema_version,
        "minimum_schema_version": expected_schema_version,
    }


def health_status(*, checked_at: datetime | None = None) -> dict[str, Any]:
    checked_at = checked_at or datetime.now(UTC)
    try:
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
    except Exception:
        return {
            "status": "degraded",
            "checked_at": checked_at.isoformat(),
            "database": "unavailable",
            "worker": worker_health({}, checked_at=checked_at),
            "trainer": trainer_health({}, checked_at=checked_at),
            "latest_scan_at": None,
            "edgar_updated_at": None,
            "scan_error": None,
        }
    workers = worker_health(states, checked_at=checked_at)
    trainer = trainer_health(states, checked_at=checked_at)
    return {
        "status": "ok" if workers["status"] == "ok" else "degraded",
        "checked_at": checked_at.isoformat(),
        "database": "ok",
        "worker": workers,
        "trainer": trainer,
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
                or deployment_health["trainer"]["status"] != "ok"
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
                "state": "disabled",
                "provider": "none",
            },
            "flash_wallet": {
                "state": "live",
                "daily_claim": DAILY_CLAIM_AMOUNT,
                "report_cost": REPORT_COST,
                "comment_cost": COMMENT_COST,
                "private_alpha_hours": REPORT_EXCLUSIVE_HOURS,
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
                "complete_groups": ranker["complete_groups"],
                "training_origins": ranker["training_origins"],
                "trainer": deployment_health["trainer"],
                "training_policy": manifest["ranker_training"],
            },
            "research": {
                "openai_available": bool(os.getenv("OPENAI_API_KEY", "")),
                "openrouter_available": (
                    NODE_SERVICE.openrouter.status()["status"] == "connected"
                ),
                "provider": FLASH.provider,
                "flash_model": FLASH.model,
                "credential_location": "server",
                "browser_key_accepted": False,
                "queue_payload": "report_id",
                "visibility": "private_for_one_hour_or_until_owner_publishes",
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


@router.get("/live")
def liveness_api() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health")
@router.get("/ready")
def readiness_api() -> JSONResponse:
    payload = readiness_status()
    return JSONResponse(payload, status_code=200 if payload["status"] == "ok" else 503)


@router.get("/health/details")
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
