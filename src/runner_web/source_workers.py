from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from runner_web.discovery_sources import (
    discovery_sources_enabled,
    discovery_watchlist,
    refresh_bluesky_social,
    refresh_gdelt_news,
)
from runner_web.nasdaq_halts import refresh_trade_halts

LOG = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")
DISCOVERY_INTERVAL_SECONDS = max(15, int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "30")))


def trade_halts_enabled() -> bool:
    return os.getenv("NASDAQ_TRADE_HALTS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def extended_us_session_is_open(value: datetime | None = None) -> bool:
    eastern = (value or datetime.now(UTC)).astimezone(EASTERN)
    minutes = eastern.hour * 60 + eastern.minute
    return eastern.weekday() < 5 and 4 * 60 <= minutes < 20 * 60


async def trading_halt_worker() -> None:
    """Poll at Nasdaq's stated maximum rate while the extended session is open."""

    while True:
        if trade_halts_enabled() and extended_us_session_is_open():
            try:
                await asyncio.to_thread(refresh_trade_halts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The failed or partial fetch is already durable in ingestion_runs.
                LOG.warning("Nasdaq halt refresh failed: %s", exc)
        await asyncio.sleep(60)


async def discovery_source_worker() -> None:
    """Rotate across 30 Pulse/Alpha symbols so each is searched about every 15 minutes."""

    await asyncio.sleep(25)
    cursor = 0
    while True:
        started = asyncio.get_running_loop().time()
        if discovery_sources_enabled():
            try:
                watchlist = await asyncio.to_thread(discovery_watchlist, 30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Discovery watchlist refresh failed: %s", exc)
                watchlist = []
            if watchlist:
                target = watchlist[cursor % len(watchlist)]
                cursor += 1
                results = await asyncio.gather(
                    asyncio.to_thread(
                        refresh_gdelt_news,
                        target["ticker"],
                        target["company"],
                    ),
                    asyncio.to_thread(refresh_bluesky_social, target["ticker"]),
                    return_exceptions=True,
                )
                for source, result in zip(("GDELT", "Bluesky"), results, strict=True):
                    if isinstance(result, Exception):
                        LOG.warning("%s discovery refresh failed: %s", source, result)
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(5, DISCOVERY_INTERVAL_SECONDS - elapsed))
