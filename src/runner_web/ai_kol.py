from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_FLASH_MODEL = "z-ai/glm-5.3"
KOL_LADDER_SIZE = 4
FLASH_VERSION_ID = os.getenv("FLASH_VERSION_ID", "flash-2026-09-b")
FLASH_VERSION_LABEL = os.getenv("FLASH_VERSION_LABEL", "Flash 2026.09b")
FLASH_PROMPT_VERSION = "daily-flash-v3"
FLASH_CONTEXT_VERSION = "identity-thesis-v1"
FLASH_RISK_POLICY_VERSION = "market_risk_v3"
FLASH_OUTPUT_SCHEMA_VERSION = "flash-report-v3"
FLASH_PIPELINE_VERSION = "one-shot-system-context-v2"
FLASH_FORECAST_CONTRACT_VERSION = "flash-next-session-v1"
SPORTS_FORECAST_CONTRACT_VERSION = "sports-moneyline-v1"


def model_display_name(model: str) -> str:
    known = {
        "z-ai/glm-5.3": "GLM 5.3",
    }
    return known.get(model, model.rsplit("/", 1)[-1])


@dataclass(frozen=True, slots=True)
class AIKol:


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



FLASH = AIKol(
    id="kol-flash",
    slot="flash",
    ladder_position=1,
    ladder_size=KOL_LADDER_SIZE,
    display_name="Flash",
    emoji="⚡",
    provider="openrouter",
    model=_FLASH_MODEL,
    description=f"Runner Watch research model: {model_display_name(_FLASH_MODEL)}.",
)


def actor_snapshot(actor: AIKol = FLASH) -> dict[str, Any]:


    return actor.snapshot()


def flash_version_snapshot(actor: AIKol = FLASH) -> dict[str, Any]:


    configuration = {
        "actor_id": actor.id,
        "provider": actor.provider,
        "requested_model": actor.model,
        "allowed_resolved_model": actor.model,
        "prompt_version": FLASH_PROMPT_VERSION,
        "context_version": FLASH_CONTEXT_VERSION,
        "risk_policy_version": FLASH_RISK_POLICY_VERSION,
        "output_schema_version": FLASH_OUTPUT_SCHEMA_VERSION,
        "pipeline_version": FLASH_PIPELINE_VERSION,
        "forecast_contract_version": FLASH_FORECAST_CONTRACT_VERSION,
    }
    fingerprint = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "id": FLASH_VERSION_ID,
        "public_label": FLASH_VERSION_LABEL,
        **configuration,
        "configuration_fingerprint": fingerprint,
    }
