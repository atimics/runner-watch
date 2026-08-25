from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from runner_web.nasdaq_halts import refresh_trade_halts

LOG = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")


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
