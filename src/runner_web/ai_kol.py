from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_FLASH_MODEL = "z-ai/glm-5.3"
KOL_LADDER_SIZE = 4


def model_display_name(model: str) -> str:
    known = {
        "z-ai/glm-5.3": "GLM 5.3",
    }
    return known.get(model, model.rsplit("/", 1)[-1])


@dataclass(frozen=True, slots=True)
class AIKol:
    """A public AI identity and its current, replaceable model assignment."""

    id: str
    slot: str
    ladder_position: int
    ladder_size: int
    display_name: str
    emoji: str
    provider: str
    model: str
    description: str

    @property
    def model_label(self) -> str:
        return model_display_name(self.model)

    def snapshot(self) -> dict[str, Any]:
        return {**asdict(self), "model_label": self.model_label}


_FLASH_MODEL = os.getenv(
    "FLASH_MODEL",
    os.getenv("OPENROUTER_RESEARCH_MODEL", DEFAULT_FLASH_MODEL),
)


# Flash is the only live ladder slot. Its identity survives future model promotions.
FLASH = AIKol(
    id="kol-flash",
    slot="flash",
    ladder_position=1,
    ladder_size=KOL_LADDER_SIZE,
    display_name="Flash",
    emoji="⚡",
    provider="openrouter",
    model=_FLASH_MODEL,
    description=(
        "Runner Watch's lead AI KOL, currently powered by "
        f"{model_display_name(_FLASH_MODEL)}."
    ),
)


def actor_snapshot(actor: AIKol = FLASH) -> dict[str, Any]:
    """Freeze public identity and model attribution onto an authored result."""

    return actor.snapshot()
