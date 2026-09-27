"""The summary both markets' ratification shares.

Each standard is met, not met, not checked yet (no data, which blocks), or not
applied (the standard does not fit this company or coin, which does not
block and is left out of the count).
"""

from __future__ import annotations

from typing import Any


def summarize(
    labels: dict[str, str],
    results: dict[str, bool | None],
    details: dict[str, str],
    *,
    not_applied: set[str] = frozenset(),  # type: ignore[assignment]
    note: str,
    as_of: str,
) -> dict[str, Any]:
    applicable = [key for key in labels if key not in not_applied]
    return {
        "ratified": all(results[key] is True for key in applicable),
        "met": sum(results[key] is True for key in applicable),
        "total": len(applicable),
        "not_applied": len(labels) - len(applicable),
        "standards": [
            {
                "key": key,
                "label": labels[key],
                "met": None if key in not_applied else results[key],
                "applies": key not in not_applied,
                "detail": details[key],
            }
            for key in labels
        ],
        "note": note,
        "as_of": as_of,
    }
