from __future__ import annotations

import resource
import sys
import threading
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

_SAMPLE_LIMIT = 512
_LOCK = threading.Lock()
_ROUTES: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=_SAMPLE_LIMIT))
_ROUTE_COUNTS: dict[str, int] = defaultdict(int)
_CACHES: dict[str, dict[str, int | float]] = defaultdict(
    lambda: {"hit": 0, "miss": 0, "stale": 0, "shared": 0, "wait": 0, "build_ms": 0.0}
)
_DATABASE_WAITS: deque[float] = deque(maxlen=_SAMPLE_LIMIT)


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def record_route(method: str, route: str, duration_ms: float) -> None:
    key = f"{method.upper()} {route}"
    with _LOCK:
        _ROUTES[key].append(duration_ms)
        _ROUTE_COUNTS[key] += 1


def record_cache(cache: str, event: str, *, duration_ms: float = 0.0) -> None:
    with _LOCK:
        bucket = _CACHES[cache]
        bucket[event] = int(bucket.get(event, 0)) + 1
        if duration_ms:
            bucket["build_ms"] = float(bucket.get("build_ms", 0.0)) + duration_ms


def record_database_wait(duration_ms: float) -> None:
    with _LOCK:
        _DATABASE_WAITS.append(duration_ms)


def _latencies(samples: deque[float]) -> dict[str, float | int]:
    values = list(samples)
    return {
        "samples": len(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": round(max(values), 2) if values else 0.0,
    }


def performance_snapshot() -> dict[str, Any]:
    with _LOCK:
        routes = {
            key: {"requests": _ROUTE_COUNTS[key], **_latencies(samples)}
            for key, samples in sorted(_ROUTES.items())
        }
        caches = {
            key: {
                **values,
                "build_ms": round(float(values.get("build_ms", 0.0)), 2),
            }
            for key, values in sorted(_CACHES.items())
        }
        database_wait = _latencies(_DATABASE_WAITS)
    maximum_rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform != "darwin":
        maximum_rss *= 1024
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "process": {"maximum_rss_mb": round(maximum_rss / 1024 / 1024, 1)},
        "routes": routes,
        "caches": caches,
        "database_pool_wait": database_wait,
    }


def reset_performance_metrics() -> None:

    with _LOCK:
        _ROUTES.clear()
        _ROUTE_COUNTS.clear()
        _CACHES.clear()
        _DATABASE_WAITS.clear()
