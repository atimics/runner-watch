"""Opt-in web-process lifecycle for the local-first swarm runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping

from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.runtime import AttachedSwarmRuntime

LOG = logging.getLogger(__name__)


def open_swarm_runtime(
    env: Mapping[str, str] | None = None,
) -> AttachedSwarmRuntime | None:
    """Return no network runtime in solo mode and a complete one in attached mode."""

    config = SwarmRuntimeConfig.from_env(env)
    return AttachedSwarmRuntime.open(config) if config.attached else None


async def maintain_swarm_runtime(runtime: AttachedSwarmRuntime) -> None:
    """Renew discovery state and retry configured seeds until process shutdown."""

    while True:
        try:
            runtime.renew_manifest()
            if runtime.config.bootstrap_urls:
                await asyncio.to_thread(runtime.refresh_bootstraps)
        except Exception:
            LOG.exception("Swarm bootstrap refresh failed")
        await asyncio.sleep(runtime.config.bootstrap_interval_seconds)
