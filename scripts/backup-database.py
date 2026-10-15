"""Snapshot the running database volumes and verify new recovery points."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from typing import Any

SNAPSHOT_WAIT_SECONDS = 1800
PENDING_STATES = {"waiting", "running", "pending"}


def fly(*args: str) -> str:
    return subprocess.run(
        ["flyctl", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout


def records(*args: str) -> list[dict[str, Any]]:
    value = json.loads(fly(*args, "--json"))
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Expected a list of Fly records")
    return value


def backup_database(
    app: str, volume_name: str = "pg_data", wait_seconds: int = SNAPSHOT_WAIT_SECONDS
) -> list[str]:
    running = {
        machine["id"]
        for machine in records("machine", "list", "--app", app)
        if machine.get("state") == "started"
    }
    volumes = [
        volume
        for volume in records("volumes", "list", "--app", app)
        if volume.get("name") == volume_name
        and volume.get("state") == "created"
        and volume.get("attached_machine_id") in running
    ]
    if not volumes:
        raise RuntimeError(
            f"A running database with an attached {volume_name} volume is required for {app}"
        )
    pending: dict[str, set[str]] = {}
    for volume in volumes:
        volume_id = volume["id"]
        command = ("volumes", "snapshots", "list", volume_id, "--app", app)
        existing = records(*command)
        pending[volume_id] = {
            snapshot["id"] for snapshot in existing if snapshot.get("status") not in PENDING_STATES
        }
        if any(snapshot.get("status") in PENDING_STATES for snapshot in existing):
            print(f"Waiting for the current snapshot of {volume_id}", flush=True)
        else:
            fly("volumes", "snapshots", "create", volume_id, "--app", app)
            print(f"Snapshot scheduled for {volume_id}", flush=True)

    snapshots: dict[str, str] = {}
    deadline = time.monotonic() + wait_seconds
    while pending:
        for volume_id, previous in list(pending.items()):
            command = ("volumes", "snapshots", "list", volume_id, "--app", app)
            fresh = [snapshot for snapshot in records(*command) if snapshot["id"] not in previous]
            complete = next(
                (snapshot for snapshot in fresh if snapshot.get("status") == "created"), None
            )
            if complete:
                snapshots[volume_id] = complete["id"]
                print(f"Snapshot {complete['id']} created for {volume_id}", flush=True)
                del pending[volume_id]
                continue
            failed = next(
                (snapshot for snapshot in fresh if snapshot.get("status") in {"failed", "error"}),
                None,
            )
            if failed:
                raise RuntimeError(f"Snapshot {failed['id']} failed for {volume_id}")
        if not pending:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Snapshot completion timed out for " + ", ".join(pending))
        time.sleep(min(10, remaining))
    return [snapshots[volume["id"]] for volume in volumes]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--volume-name", default="pg_data")
    parser.add_argument("--wait-seconds", type=int, default=SNAPSHOT_WAIT_SECONDS)
    args = parser.parse_args()
    try:
        backup_database(args.app, args.volume_name, args.wait_seconds)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr.strip() or str(exc)) from exc


if __name__ == "__main__":
    main()
