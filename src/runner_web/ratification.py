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
    unratified_note: str,
    as_of: str,
) -> dict[str, Any]:
    """`note` is shown only when every standard is met; otherwise `unratified_note`.

    A failing or unchecked subject must never carry text that reads as ratified.
    """

    applicable = [key for key in labels if key not in not_applied]
    ratified = all(results[key] is True for key in applicable)
    return {
        "ratified": ratified,
        "met": sum(results[key] is True for key in applicable),
        # Not checked yet: missing data. It blocks and is never counted as met.
        "unchecked": sum(results[key] is None for key in applicable),
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
        "note": note if ratified else unratified_note,
        "as_of": as_of,
    }
