"""Check free space on every running Fly database machine before the disk fills."""

from __future__ import annotations

import argparse
import json
import subprocess


def fly(*args: str) -> str:
    return subprocess.run(
        ["flyctl", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout


def disk_usage(output: str) -> tuple[int, int]:
    """Read the /data row from portable df output, in KiB."""
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 6 and fields[-1] == "/data":
            total, used, available = map(int, fields[1:4])
            if total <= 0 or used < 0 or available < 0 or used + available > total:
                raise ValueError("Invalid database disk usage")
            return used, available
    raise ValueError("Database /data mount missing from disk usage")


def check_storage(app: str, max_used_percent: int = 80, min_free_gib: int = 5) -> list[dict]:
    if not 1 <= max_used_percent <= 99 or min_free_gib < 1:
        raise ValueError("Use a percentage from 1 to 99 and at least 1 GiB free")
    machines = json.loads(fly("machine", "list", "--app", app, "--json"))
    if not isinstance(machines, list) or any(not isinstance(row, dict) for row in machines):
        raise ValueError("Expected a list of database machines")
    running = [machine for machine in machines if machine.get("state") == "started"]
    if not running:
        raise RuntimeError("A running database machine is required")
    results, failures = [], []
    for machine in running:
        identifier = machine["id"]
        used, available = disk_usage(
            fly("ssh", "console", "--app", app, "--machine", identifier, "-C", "df -Pk /data")
        )
        # Match df's usable capacity, excluding blocks reserved for the system.
        percent = 100 * used / (used + available) if used + available else 100
        free_gib = available / 1024**2
        result = {
            "machine": identifier,
            "used_percent": round(percent, 1),
            "free_gib": round(free_gib, 2),
        }
        results.append(result)
        print(json.dumps(result), flush=True)
        if percent >= max_used_percent or free_gib < min_free_gib:
            failures.append(identifier)
    if failures:
        raise RuntimeError("Database storage needs attention: " + ", ".join(failures))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--max-used-percent", type=int, default=80)
    parser.add_argument("--min-free-gib", type=int, default=5)
    args = parser.parse_args()
    try:
        check_storage(args.app, args.max_used_percent, args.min_free_gib)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr.strip() or str(exc)) from exc


if __name__ == "__main__":
    main()
