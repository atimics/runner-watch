"""Snapshot the attached database volumes and verify new recovery points."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from typing import Any


def fly(*args: str) -> str:
    return subprocess.run(
        ["flyctl", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout


def records(*args: str) -> list[dict[str, Any]]:
    value = json.loads(fly(*args, "--json"))
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Expected a list of Fly records")
    return value


def backup_database(app: str, volume_name: str = "pg_data", wait_seconds: int = 600) -> list[str]:
    volumes = [
        volume
        for volume in records("volumes", "list", "--app", app)
        if volume.get("name") == volume_name
        and volume.get("state") == "created"
        and volume.get("attached_machine_id")
    ]
    if not volumes:
        raise RuntimeError(f"An attached {volume_name} volume is required for {app}")
    snapshots = []
    for volume in volumes:
        volume_id = volume["id"]
        command = ("volumes", "snapshots", "list", volume_id, "--app", app)
        previous = {snapshot["id"] for snapshot in records(*command)}
        fly("volumes", "snapshots", "create", volume_id, "--app", app)
        print(f"Snapshot scheduled for {volume_id}", flush=True)
        deadline = time.monotonic() + wait_seconds
        while True:
            fresh = [snapshot for snapshot in records(*command) if snapshot["id"] not in previous]
            complete = next(
                (snapshot for snapshot in fresh if snapshot.get("status") == "created"), None
            )
            if complete:
                snapshots.append(complete["id"])
                print(f"Snapshot {complete['id']} created for {volume_id}", flush=True)
                break
            failed = next(
                (snapshot for snapshot in fresh if snapshot.get("status") in {"failed", "error"}),
                None,
            )
            if failed:
                raise RuntimeError(f"Snapshot {failed['id']} failed for {volume_id}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Snapshot completion timed out for {volume_id}")
            time.sleep(min(10, remaining))
    return snapshots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--volume-name", default="pg_data")
    parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args()
    try:
        backup_database(args.app, args.volume_name, args.wait_seconds)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr.strip() or str(exc)) from exc


if __name__ == "__main__":
    main()
