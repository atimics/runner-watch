"""Declared inputs for a market report.

A report stores the inputs it was built from, plus a version token over them.
A sources footer can then be drawn from this record, so it cannot drift from
what the report used. Nothing here is shown to readers yet.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# How a fact was obtained. Kept small on purpose.
STATUS_OBSERVED = "observed"

_SCAN_LABELS = {
    "source": "Scanner run behind this report",
    "comparison": "Earlier scanner run used for comparison",
}


def _scan_source(role: str, scan_run_id: str, as_of: str | None) -> dict[str, Any]:
    return {
        "kind": "scan_run",
        "role": role,
        "ref": scan_run_id,
        "label": _SCAN_LABELS[role],
        "as_of": as_of,
        "status": STATUS_OBSERVED,
    }


def report_sources(
    payload: dict[str, Any], comparison_as_of: str | None = None
) -> list[dict[str, Any]]:
    """List the inputs of a report payload, most important first."""

    sources: list[dict[str, Any]] = []
    source_run = payload.get("source_scan_run_id")
    if source_run:
        sources.append(_scan_source("source", str(source_run), payload.get("as_of")))
    comparison_run = payload.get("comparison_scan_run_id")
    if comparison_run and comparison_run != source_run:
        sources.append(_scan_source("comparison", str(comparison_run), comparison_as_of))
    return sources


def version_token(sources: list[dict[str, Any]]) -> str:
    """A stable token: the same inputs always give the same token."""

    inputs = sorted(
        (str(item.get("kind")), str(item.get("role")), str(item.get("ref"))) for item in sources
    )
    digest = hashlib.sha256(json.dumps(inputs, separators=(",", ":")).encode()).hexdigest()
    return digest[:16]


def encode_sources(sources: list[dict[str, Any]]) -> str:
    return json.dumps(sources, separators=(",", ":"))


def decode_sources(value: Any) -> list[dict[str, Any]]:
    """Read a stored record. Reports saved before sources existed give an empty list."""

    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]
