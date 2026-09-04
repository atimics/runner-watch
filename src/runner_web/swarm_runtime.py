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

    config = SwarmRuntimeConfig.from_env(env)
    return AttachedSwarmRuntime.open(config) if config.attached else None


async def maintain_swarm_runtime(runtime: AttachedSwarmRuntime) -> None:

    async def renew_manifest() -> None:
        interval = max(15, runtime.config.manifest_ttl_seconds // 3)
        while True:
            try:
                runtime.renew_manifest()
            except Exception:
                LOG.exception("Swarm manifest renewal failed")
            await asyncio.sleep(interval)

    async def refresh_bootstraps() -> None:
        while True:
            if runtime.config.bootstrap_urls:
                try:
                    await asyncio.to_thread(runtime.refresh_bootstraps)
                except Exception:
                    LOG.exception("Swarm bootstrap refresh failed")
            await asyncio.sleep(runtime.config.bootstrap_interval_seconds)

    await asyncio.gather(renew_manifest(), refresh_bootstraps())
