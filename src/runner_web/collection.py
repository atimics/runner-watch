from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from runner_watch.ingestion import SourceFetch
from runner_watch.market_data import RoutedMarketData, routed_market_data
from runner_web.ingestion import record_source_fetch


def record_market_bars(interval: str, frames: dict[str, pd.DataFrame]) -> None:


    timestamp = datetime.now(UTC)
    record_source_fetch(
        SourceFetch.success(
            source="yahoo",
            feed="market_bars",
            locator=f"yfinance://download/{interval}",
            started_at=timestamp,
            payload=frames,
            content_type="application/x-pandas-frames",
            metadata={
                "interval": interval,
                "requested_tickers": sorted(frames),
                "returned_tickers": sorted(frames),
                "missing_tickers": [],
            },
        )
    )


def record_source_document(url: str, body: bytes) -> None:


    timestamp = datetime.now(UTC)
    stripped = body.lstrip()
    content_type = (
        "application/json"
        if stripped.startswith((b"{", b"["))
        else "application/xml"
        if stripped.startswith(b"<")
        else "application/octet-stream"
    )
    record_source_fetch(
        SourceFetch.success(
            source="sec",
            feed="document",
            locator=url,
            started_at=timestamp,
            payload=body,
            content_type=content_type,
        )
    )


def recording_market_data(batch_size: int = 60, timeout: float = 15.0) -> RoutedMarketData:
    return routed_market_data(
        batch_size=batch_size,
        timeout=timeout,
        fetch_recorder=record_source_fetch,
    )
