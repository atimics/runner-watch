"""Team identity checks shared by collection, screens, and Calls."""

from __future__ import annotations

from typing import Any

PENDING_TEAM_NAMES = {"", "tbd", "tba", "unknown", "—", "-", "to be determined", "to be announced"}


def participants_ready(event: dict[str, Any]) -> bool:
    for side in ("away", "home"):
        team = event.get(side) or {}
        identifier = team.get("id", event.get(f"{side}_team_id"))
        if identifier is not None:
            value = str(identifier).strip().casefold()
            if value in PENDING_TEAM_NAMES or (value.lstrip("-").isdigit() and int(value) <= 0):
                return False
        labels = [
            team.get("abbreviation", event.get(f"{side}_abbreviation")),
            team.get("name", event.get(f"{side}_team_name")),
        ]
        present = [str(label).strip().casefold() for label in labels if str(label or "").strip()]
        if not present or any(label in PENDING_TEAM_NAMES for label in present):
            return False
    return True
