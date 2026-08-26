#!/usr/bin/env python3
"""Run small, bounded HTTP bursts against a staging deployment."""

from __future__ import annotations

import argparse
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from threading import Barrier


@dataclass(frozen=True, slots=True)
class Sample:
    status: int
    seconds: float
    bytes_read: int
    error: str | None = None


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def request_once(url: str, barrier: Barrier, timeout: float) -> Sample:
    barrier.wait()
    started = time.perf_counter()
    request = urllib.request.Request(
        url,
        headers={
            "Accept-Encoding": "gzip",
            "User-Agent": "RunnerWatch-Scalability-Probe/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = response.read()
            return Sample(
                status=int(response.status),
                seconds=time.perf_counter() - started,
                bytes_read=len(payload),
            )
    except urllib.error.HTTPError as exc:
        exc.read()
        return Sample(
            status=int(exc.code),
            seconds=time.perf_counter() - started,
            bytes_read=0,
            error=str(exc),
        )
    except Exception as exc:
        return Sample(
            status=0,
            seconds=time.perf_counter() - started,
            bytes_read=0,
            error=str(exc),
        )


def burst(base_url: str, path: str, concurrency: int, timeout: float) -> dict[str, object]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    barrier = Barrier(concurrency)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        samples = list(
            pool.map(
                lambda _: request_once(url, barrier, timeout),
                range(concurrency),
            )
        )
    wall_seconds = time.perf_counter() - started
    durations_ms = [sample.seconds * 1000 for sample in samples]
    statuses: dict[str, int] = {}
    for sample in samples:
        key = str(sample.status)
        statuses[key] = statuses.get(key, 0) + 1
    return {
        "path": path,
        "concurrency": concurrency,
        "requests": len(samples),
        "wall_seconds": round(wall_seconds, 3),
        "requests_per_second": round(len(samples) / max(wall_seconds, 0.001), 2),
        "p50_ms": round(percentile(durations_ms, 0.50), 1),
        "p95_ms": round(percentile(durations_ms, 0.95), 1),
        "maximum_ms": round(max(durations_ms, default=0.0), 1),
        "statuses": statuses,
        "bytes_read": sum(sample.bytes_read for sample in samples),
        "errors": [asdict(sample) for sample in samples if sample.error][:5],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run bounded 20, 40, and 80 request bursts against staging."
    )
    parser.add_argument("base_url", help="Staging base URL, for example https://staging.test")
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help="Path to test. Repeat for more paths. Defaults to /health and /api/pulse?limit=20.",
    )
    parser.add_argument("--levels", default="20,40,80")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--allow-production",
        action="store_true",
        help="Allow a host whose name does not look like staging or localhost.",
    )
    arguments = parser.parse_args()
    parsed = urllib.parse.urlparse(arguments.base_url)
    host = (parsed.hostname or "").lower()
    safe_host = host in {"localhost", "127.0.0.1", "::1"} or any(
        marker in host for marker in ("staging", "stage", "preview")
    )
    if not safe_host and not arguments.allow_production:
        parser.error("Refusing a non-staging host without --allow-production")
    levels = [int(value) for value in arguments.levels.split(",") if value.strip()]
    if not levels or any(value < 1 or value > 100 for value in levels):
        parser.error("Concurrency levels must be between 1 and 100")
    paths = arguments.path or ["/health", "/api/pulse?limit=20"]
    results = [
        burst(arguments.base_url, path, concurrency, arguments.timeout)
        for path in paths
        for concurrency in levels
    ]
    print(json.dumps({"base_url": arguments.base_url, "results": results}, indent=2))


if __name__ == "__main__":
    main()
