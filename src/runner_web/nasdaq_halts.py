from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_watch.xml_security import read_limited, safe_xml_fromstring
from runner_web.ingestion import record_source_batch, record_source_fetch

TRADE_HALT_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
TRADE_HALT_PAGE = "https://www.nasdaqtrader.com/Trader.aspx?id=TradeHaltRSS"
USER_AGENT = "RunnerWatch/0.2 https://stonks.rati.foundation"
EASTERN = ZoneInfo("America/New_York")
MAX_TRADE_HALT_RESPONSE_BYTES = 2 * 1024 * 1024
Download = Callable[[str, float], tuple[bytes, str | None]]


def _text(value: str | None) -> str:
    return (value or "").strip()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _fields(item: ET.Element) -> dict[str, str]:
    return {_local_name(child.tag): _text(child.text) for child in item}


def _parse_eastern(date_text: str, time_text: str) -> datetime | None:
    if not date_text or not time_text:
        return None
    for date_format in ("%m/%d/%Y", "%Y-%m-%d"):
        for time_format in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
            try:
                value = datetime.strptime(
                    f"{date_text} {time_text}",
                    f"{date_format} {time_format}",
                )
            except ValueError:
                continue
            return value.replace(tzinfo=EASTERN).astimezone(UTC)
    raise ValueError(f"Unknown Nasdaq halt timestamp: {date_text} {time_text}")


def _published_at(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def parse_trade_halts(body: bytes) -> tuple[MarketEvent, ...]:
    """Parse the Nasdaq RSS response without depending on its namespace prefix."""

    root = safe_xml_fromstring(body, max_bytes=MAX_TRADE_HALT_RESPONSE_BYTES)
    events: list[MarketEvent] = []
    for item in (element for element in root.iter() if _local_name(element.tag) == "item"):
        values = _fields(item)
        ticker = values.get("issuesymbol", "").upper()
        halt_date = values.get("haltdate", "")
        halt_time = values.get("halttime", "")
        if not ticker or not halt_date or not halt_time:
            raise ValueError("Nasdaq halt item is missing symbol, halt date, or halt time")
        halted_at = _parse_eastern(halt_date, halt_time)
        if halted_at is None:
            raise ValueError("Nasdaq halt item has no usable halt time")
        resume_date = values.get("resumptiondate") or halt_date
        quote_resume_at = _parse_eastern(
            resume_date,
            values.get("resumptionquotetime", ""),
        )
        trade_resume_at = _parse_eastern(
            resume_date,
            values.get("resumptiontradetime", ""),
        )
        reason_code = values.get("reasoncode", "")
        status = "resume_announced" if trade_resume_at else "halted"
        payload: dict[str, Any] = {
            "issue_name": values.get("issuename") or values.get("title"),
            "market": values.get("market") or values.get("mkt"),
            "reason_code": reason_code,
            "pause_threshold_price": values.get("pausethresholdprice") or None,
            "quote_resume_at": quote_resume_at.isoformat() if quote_resume_at else None,
            "trade_resume_at": trade_resume_at.isoformat() if trade_resume_at else None,
        }
        events.append(
            MarketEvent(
                event_id=f"{ticker}:{halted_at.isoformat()}",
                ticker=ticker,
                event_type="trading_halt",
                event_at=halted_at,
                effective_at=halted_at,
                published_at=_published_at(values.get("pubdate", "")),
                status=status,
                source_url=TRADE_HALT_PAGE,
                payload=payload,
            )
        )
    return tuple(events)


def _download(url: str, timeout: float) -> tuple[bytes, str | None]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        content_type = response.headers.get_content_type()
        return read_limited(response, max_bytes=MAX_TRADE_HALT_RESPONSE_BYTES), content_type


def refresh_trade_halts(
    *,
    timeout: float = 10,
    download: Download = _download,
) -> dict[str, Any]:
    """Fetch, archive, parse, and normalize the current Nasdaq halt feed."""

    started_at = datetime.now(UTC)
    try:
        body, content_type = download(TRADE_HALT_URL, timeout)
    except Exception as exc:
        run_id = record_source_fetch(
            SourceFetch.failure(
                source="nasdaq_trader",
                feed="trade_halts",
                locator=TRADE_HALT_URL,
                started_at=started_at,
                error=exc,
                metadata={"requested_count": 1},
            )
        )
        raise RuntimeError(f"Nasdaq halt fetch failed in run {run_id}: {exc}") from exc

    try:
        events = parse_trade_halts(body)
    except Exception as exc:
        run_id = record_source_fetch(
            SourceFetch.success(
                source="nasdaq_trader",
                feed="trade_halts",
                locator=TRADE_HALT_URL,
                started_at=started_at,
                payload=body,
                content_type=content_type or "application/xml",
                metadata={
                    "requested_count": 1,
                    "received_count": 0,
                    "parse_error": str(exc)[:500],
                },
                partial=True,
            )
        )
        raise RuntimeError(f"Nasdaq halt parse failed in run {run_id}: {exc}") from exc

    fetch = SourceFetch.success(
        source="nasdaq_trader",
        feed="trade_halts",
        locator=TRADE_HALT_URL,
        started_at=started_at,
        payload=body,
        content_type=content_type or "application/xml",
        metadata={"requested_count": 1, "received_count": len(events)},
    )
    run_id = record_source_batch(SourceBatch(fetch=fetch, market_events=events))
    return {"run_id": run_id, "events": len(events), "status": fetch.status}
