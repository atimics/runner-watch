from __future__ import annotations

from .operations import health_status


def background_processes_healthy() -> bool:
    payload = health_status()
    return (
        payload.get("worker", {}).get("status") == "ok"
        and payload.get("trainer", {}).get("status") == "ok"
    )


def main() -> None:
    if not background_processes_healthy():
        raise SystemExit("Background process health check failed")


if __name__ == "__main__":
    main()
