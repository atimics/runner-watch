from __future__ import annotations

from typing import Any

from .operations import health_status


def _component_healthy(component: dict[str, Any]) -> bool:
    status = str(component.get("status") or "")
    if status == "ok":
        return True
    if status != "degraded":
        return False
    instances = (component.get("detail") or {}).get("instances") or []
    if not instances:
        return False
    if any(str(instance.get("status") or "") == "stale" for instance in instances):
        return False
    if any(
        instance.get("missing_workers") or instance.get("failed_workers") for instance in instances
    ):
        return False
    # A worker whose progress key stopped advancing is an ops alert, not a
    # reason to roll back a deploy: the fix for it may be the deploy itself.
    return any(instance.get("stale_workers") for instance in instances)


def background_processes_healthy() -> bool:
    payload = health_status()
    return _component_healthy(payload.get("worker", {})) and _component_healthy(
        payload.get("trainer", {})
    )


def main() -> None:
    if not background_processes_healthy():
        raise SystemExit("Background process health check failed")


if __name__ == "__main__":
    main()
