from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from runner_web.congressional_disclosures import (
    house_disclosures_enabled,
    refresh_house_disclosures,
)
from runner_web.discovery_sources import (
    apewisdom_social_enabled,
    bluesky_search_enabled,
    discovery_sources_enabled,
    discovery_watchlist,
    gdelt_news_enabled,
    refresh_apewisdom_social,
    refresh_bluesky_social,
    refresh_gdelt_news,
    refresh_yahoo_news,
    yahoo_news_enabled,
)
from runner_web.free_risk_sources import (
    free_legal_sources_enabled,
    refresh_free_legal_sources,
)
from runner_web.nasdaq_halts import refresh_trade_halts

LOG = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")
DISCOVERY_INTERVAL_SECONDS = max(15, int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "30")))
FREE_LEGAL_INTERVAL_SECONDS = max(
    3_600,
    int(os.getenv("FREE_LEGAL_INTERVAL_SECONDS", "86400")),
)
HOUSE_DISCLOSURE_INTERVAL_SECONDS = max(
    300,
    int(os.getenv("HOUSE_DISCLOSURE_INTERVAL_SECONDS", "900")),
)


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


async def house_disclosure_worker() -> None:
    """Poll the free official House index and fetch only unseen PTR documents."""

    await asyncio.sleep(35)
    while True:
        started = asyncio.get_running_loop().time()
        if house_disclosures_enabled():
            try:
                await asyncio.to_thread(refresh_house_disclosures)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("House disclosure refresh failed: %s", exc)
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(30, HOUSE_DISCLOSURE_INTERVAL_SECONDS - elapsed))


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
                calls = []
                labels = []
                if yahoo_news_enabled():
                    calls.append(
                        asyncio.to_thread(
                            refresh_yahoo_news,
                            target["ticker"],
                            target["company"],
                        )
                    )
                    labels.append("Yahoo news")
                if gdelt_news_enabled():
                    calls.append(
                        asyncio.to_thread(
                            refresh_gdelt_news,
                            target["ticker"],
                            target["company"],
                        )
                    )
                    labels.append("GDELT")
                if bluesky_search_enabled():
                    calls.append(asyncio.to_thread(refresh_bluesky_social, target["ticker"]))
                    labels.append("Bluesky")
                results = await asyncio.gather(
                    *calls,
                    return_exceptions=True,
                )
                for source, result in zip(labels, results, strict=True):
                    if isinstance(result, Exception):
                        LOG.warning("%s discovery refresh failed: %s", source, result)
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(max(5, DISCOVERY_INTERVAL_SECONDS - elapsed))


async def apewisdom_source_worker() -> None:
    """Refresh slower aggregate sources without adding another process worker."""

    await asyncio.sleep(15)
    next_legal_refresh = 0.0
    while True:
        if apewisdom_social_enabled():
            try:
                watchlist = await asyncio.to_thread(discovery_watchlist, 30)
                if watchlist:
                    await asyncio.to_thread(refresh_apewisdom_social, watchlist)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("ApeWisdom social refresh failed: %s", exc)
        loop_time = asyncio.get_running_loop().time()
        if free_legal_sources_enabled() and loop_time >= next_legal_refresh:
            try:
                await asyncio.to_thread(refresh_free_legal_sources)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Free legal source refresh failed: %s", exc)
            finally:
                next_legal_refresh = loop_time + FREE_LEGAL_INTERVAL_SECONDS
        await asyncio.sleep(900)
