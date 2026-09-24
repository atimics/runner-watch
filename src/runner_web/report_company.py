"""Small, source-linked company context saved with a daily edition."""

from __future__ import annotations

import gzip
import io
import re
import zlib
from datetime import UTC
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

from runner_web.report_spotlight import moment, number, source_url, time_label, timely_quote


class _Blocks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "h1", "h2", "h3", "br", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def business_excerpt(raw: bytes, encoding: str) -> str | None:
    """Extract a bounded paragraph after an annual report's Business heading."""
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(2_000_001)
        except (OSError, EOFError, zlib.error):
            return None
    if len(raw) > 2_000_000:
        return None
    parser = _Blocks()
    parser.feed(raw.decode("utf-8", errors="replace"))
    text = "\n".join(
        " ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip()
    )
    # Skip table-of-contents entries: require a substantive nearby paragraph.
    for heading in re.finditer(r"^Item\s+1\s*[.:-]?\s*Business[ .:-]*$", text, re.I | re.M):
        section = re.split(r"\bItem\s+1[A-C]\b", text[heading.end() :], maxsplit=1, flags=re.I)[0]
        for paragraph in section[:5000].splitlines()[:10]:
            if len(paragraph) >= 160 and re.search(r"\b(we|our|company)\b", paragraph, re.I):
                if len(paragraph) > 650:
                    paragraph = paragraph[:647].rsplit(" ", 1)[0] + "…"
                return paragraph
    return None


def freeze_company_context(database: Any, feature: dict[str, Any]) -> None:
    ticker, as_of, captured_at = feature["ticker"], feature["as_of"], feature["captured_at"]
    annual = database.execute(
        "SELECT form,title,filed_at,filing_url FROM sec_filings WHERE ticker=? "
        "AND form IN ('10-K','10-K/A') AND filed_at<=? AND created_at<=? "
        "ORDER BY filed_at DESC,accession DESC LIMIT 1",
        (ticker, as_of, captured_at),
    ).fetchone()
    business = None
    if annual and (url := source_url(annual["filing_url"])):
        prefix = url.rsplit("/", 1)[0] + "/"
        documents = database.execute(
            "SELECT source_url,content,content_encoding,content_hash FROM source_documents "
            "WHERE source='sec' AND source_url LIKE ? AND first_collected_at<=? "
            "ORDER BY first_collected_at DESC,source_url,content_hash LIMIT 8",
            (prefix + "%", captured_at),
        ).fetchall()
        for doc in documents:
            excerpt = business_excerpt(bytes(doc["content"]), doc["content_encoding"])
            if excerpt and source_url(doc["source_url"]):
                business = {
                    "text": excerpt,
                    "source_url": doc["source_url"],
                    "filed_at": annual["filed_at"],
                    "form": annual["form"],
                    "content_hash": doc["content_hash"],
                }
                break
    feature["business"] = business
    feature["annual_report"] = (
        {**dict(annual), "filing_url": source_url(annual["filing_url"])} if annual else None
    )
    # A filing on the session date is event context; causality remains a research question.
    feature["session_events"] = [
        filing for filing in feature["filings"] if str(filing["filed_at"])[:10] == as_of[:10]
    ]
    start = moment(as_of)
    if start is None:
        feature["price_trace"] = []
        return
    start = start.astimezone(ZoneInfo("America/New_York")).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    rows = database.execute(
        "SELECT id,price,quote_time,captured_at FROM scan_snapshots "
        "WHERE ticker=? AND captured_at>=? AND captured_at<=? "
        "ORDER BY captured_at,id LIMIT 256",
        (ticker, start.astimezone(UTC).isoformat(), as_of),
    ).fetchall()
    trace: dict[str, Any] = {}
    for raw in rows:
        row = dict(raw)
        quote = moment(row["quote_time"])
        if (
            (number(row["price"]) or 0) > 0
            and quote
            and quote >= start
            and timely_quote(row, row["captured_at"])
        ):
            trace[quote.astimezone(UTC).isoformat()] = {
                **row,
                "quote_time": quote.astimezone(UTC).isoformat(),
                "time_label": time_label(quote.astimezone(UTC).isoformat()),
            }
    feature["price_trace"] = sorted(trace.values(), key=lambda row: row["quote_time"])


def price_chart(trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(trace) < 2:
        return None
    prices = [number(row.get("price")) for row in trace]
    times = [moment(row.get("quote_time")) for row in trace]
    if any(p is None for p in prices) or any(t is None for t in times):
        return None
    low, high = min(prices), max(prices)
    duration = (times[-1] - times[0]).total_seconds()
    if duration <= 0:
        return None
    points = " ".join(
        f"{8 + (t - times[0]).total_seconds() / duration * 584:.1f},"
        f"{100 - (p - low) / (high - low) * 80 if high > low else 60:.1f}"
        for p, t in zip(prices, times, strict=True)
    )
    return {"points": points, "low": low, "high": high, "first": trace[0], "last": trace[-1]}
