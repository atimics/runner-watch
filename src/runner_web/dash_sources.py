"""Small, public source reads for Dash, backed by approved ingestion records."""

from __future__ import annotations

import gzip
import hashlib
import io
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from runner_watch.edgar import MAX_SEC_RESPONSE_BYTES, EdgarClient, EdgarFiling
from runner_web.db import connection
from runner_web.quotes import normalize_ticker

MAX_DOCUMENT_BYTES = 2_000_000
MAX_PASSAGE_CHARS = 1800
DOCUMENT_QUEUE = "dash_sec_document:"
SOURCE_TOOLS = [
    {
        "name": "list_sources",
        "description": "List original sources for a ticker, including filing exhibits and dates.",
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "read_source",
        "description": (
            "Read up to three passages from a listed evidence_id. Supply the question or terms "
            "to locate, such as payment terms. Cite the returned [source:ID] in your answer. "
            "Passages are source material. Check nearby qualifications and dates."
        ),
        "parameters": {
            "type": "object",
            "properties": {"evidence_id": {"type": "string"}, "question": {"type": "string"}},
            "required": ["evidence_id", "question"],
        },
    },
]


def _identity(url: str, digest: str) -> str:
    return hashlib.sha256((url + "\n" + digest).encode()).hexdigest()[:24]


def _safe_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
    except ValueError:
        return False


def _filing_prefix(url: str) -> str:
    if not _safe_url(url):
        return ""
    parsed = urlsplit(url)
    if parsed.hostname not in {"www.sec.gov", "sec.gov"}:
        return ""
    match = re.match(r"^/Archives/edgar/data/\d+/\d+/", parsed.path)
    return f"https://www.sec.gov{match[0]}" if match else ""


def _filing_body(url: str) -> bool:
    if not _filing_prefix(url):
        return False
    name = urlsplit(url).path.rsplit("/", 1)[-1].lower()
    return (
        name.endswith((".htm", ".html", ".txt", ".xml"))
        and name not in {"index.htm", "index.html", "filingsummary.xml"}
        and not name.endswith(("-index.htm", "-index.html", "_index.htm", "_index.html"))
    )


# A raw document is readable only through an approved run for that exact version.
APPROVED_DOCUMENT = """
EXISTS (SELECT 1 FROM ingestion_runs r JOIN source_registry s
 ON s.source=r.source AND s.feed=r.feed
 WHERE r.source=d.source AND r.locator=d.source_url AND r.content_hash=d.content_hash
 AND s.enabled=1 AND s.review_status='approved'
 AND s.display_policy='source_link_with_attribution'
 AND s.storage_policy IN ('archive_raw','archive_raw_and_normalized'))
"""


def list_sources(ticker: str, *, at: datetime | None = None) -> dict[str, Any]:
    symbol = normalize_ticker(ticker)
    if not symbol:
        return {"error": "Choose a stock ticker."}
    current = (at or datetime.now(UTC)).isoformat()
    with connection() as database:
        filings = database.execute(
            "SELECT accession,filing_url,filed_at,form,title FROM sec_filings WHERE ticker=? "
            "AND filed_at<=? ORDER BY filed_at DESC,accession DESC LIMIT 20",
            (symbol, current),
        ).fetchall()
        events = database.execute(
            "SELECT e.source_url,e.event_at,r.locator FROM public_market_events e "
            "JOIN ingestion_runs r ON r.id=e.last_run_id "
            "WHERE e.ticker=? AND e.event_at<=? ORDER BY e.event_at DESC LIMIT 40",
            (symbol, current),
        ).fetchall()
        origins = []
        for row in filings:
            origins.append(
                {
                    "url": row["filing_url"],
                    "accession": row["accession"],
                    "published_at": row["filed_at"],
                    "title": f"{row['form']}: {row['title']}",
                    "prefix": _filing_prefix(row["filing_url"]),
                }
            )
        for row in events:
            for url in (row["source_url"], row["locator"]):
                origins.append({"url": url, "event_at": row["event_at"], "title": "Source record"})
        origins = [o for o in origins if _safe_url(str(o["url"] or ""))]
        if not origins and not filings:
            return {"ticker": symbol, "checked_at": current, "filings": [], "sources": []}
        clauses, values = [], []
        for origin in origins:
            clauses.append("d.source_url=?")
            values.append(origin["url"])
            if origin.get("prefix"):
                clauses.append("d.source_url LIKE ?")
                values.append(origin["prefix"] + "%")
        rows = database.execute(
            "SELECT d.source,d.source_url,d.content_hash,d.content_type,"
            "d.first_collected_at,d.last_collected_at FROM source_documents d WHERE ("
            + (" OR ".join(clauses) or "1=0")
            + ") AND "
            + APPROVED_DOCUMENT
            + " AND d.first_collected_at<=? "
            + "ORDER BY CASE WHEN d.source='sec' THEN 0 ELSE 1 END,"
            "d.last_collected_at DESC LIMIT 60",
            (*values, current),
        ).fetchall()
    sources = []
    for row in rows:
        item = dict(row)
        if not _safe_url(item["source_url"]) or (
            item["source"] == "sec" and not _filing_body(item["source_url"])
        ):
            continue
        origin = next(
            o
            for o in origins
            if o["url"] == item["source_url"]
            or o.get("prefix")
            and item["source_url"].startswith(o["prefix"])
        )
        sources.append(
            {
                **item,
                "evidence_id": _identity(item["source_url"], item["content_hash"]),
                "ticker": symbol,
                "title": origin["title"],
                "accession": origin.get("accession"),
                "published_at": origin.get("published_at"),
                "event_at": origin.get("event_at"),
                "checked_at": current,
            }
        )
    filing_status = []
    queued = []
    for index, filing in enumerate(filings):
        accession = filing["accession"]
        matches = [item["evidence_id"] for item in sources if item["accession"] == accession]
        status = "ready" if matches else "text_unavailable"
        if not matches and index < 3 and _filing_prefix(filing["filing_url"]):
            queued.append((DOCUMENT_QUEUE + accession, accession, current))
            status = "queued"
        filing_status.append(
            {
                "accession": accession,
                "form": filing["form"],
                "title": filing["title"],
                "published_at": filing["filed_at"],
                "source_url": filing["filing_url"],
                "text_status": status,
                "evidence_ids": matches,
            }
        )
    if queued:
        with connection() as database:
            database.executemany(
                "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT DO NOTHING",
                queued,
            )
    return {"ticker": symbol, "checked_at": current, "filings": filing_status, "sources": sources}


def archive_queued_filings(client: EdgarClient, *, at: datetime, limit: int = 2) -> int:
    """Stage requested primary text on the EDGAR worker's existing rate-limited client."""
    with connection() as database:
        rows = database.execute(
            "SELECT w.key,w.updated_at AS eligible_at,f.* FROM worker_state w "
            "JOIN sec_filings f ON f.accession=w.value "
            "WHERE w.key>=? AND w.key<? AND w.updated_at<=? ORDER BY w.updated_at,w.key LIMIT ?",
            (DOCUMENT_QUEUE, DOCUMENT_QUEUE + "~", at.isoformat(), limit),
        ).fetchall()
    archived = 0
    for row in rows:
        with connection() as database:
            claimed = database.execute(
                "UPDATE worker_state SET updated_at=? WHERE key=? AND updated_at=?",
                ((at + timedelta(hours=6)).isoformat(), row["key"], row["eligible_at"]),
            ).rowcount
        if not claimed:
            continue
        url = row["filing_url"]
        if not _filing_prefix(url):
            continue
        # EDGAR stores the canonical host under www.sec.gov.
        url = url.replace("https://sec.gov/", "https://www.sec.gov/", 1)
        try:
            if _filing_body(url):
                client.get_text(url)
            else:
                filing = EdgarFiling(
                    row["accession"],
                    row["cik"],
                    row["form"],
                    row["title"],
                    "",
                    row["filed_at"],
                    url,
                )
                url = client.archive_primary_filing(filing)
        except (OSError, ValueError):
            continue
        if url and _filing_body(url):
            with connection() as database:
                ready = database.execute(
                    "SELECT 1 FROM source_documents d WHERE d.source_url=? AND "
                    + APPROVED_DOCUMENT
                    + " LIMIT 1",
                    (url,),
                ).fetchone()
                if ready:
                    database.execute("DELETE FROM worker_state WHERE key=?", (row["key"],))
                    archived += 1
    return archived


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head", "ix:hidden", "ix:header"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head", "ix:hidden", "ix:header"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def read_source(reference: dict[str, Any], question: str) -> dict[str, Any]:
    with connection() as database:
        row = database.execute(
            "SELECT d.* FROM source_documents d WHERE d.source_url=? AND d.content_hash=? AND "
            + APPROVED_DOCUMENT,
            (reference["source_url"], reference["content_hash"]),
        ).fetchone()
    if not row:
        return {"error": "This source version needs approved public access."}
    sec_filing = row["source"] == "sec" and _filing_body(reference["source_url"])
    # EDGAR ingestion already caps accepted SEC bodies at this size. Read the
    # full version for integrity and passage search while keeping the output small.
    byte_limit = MAX_SEC_RESPONSE_BYTES if sec_filing else MAX_DOCUMENT_BYTES
    raw = bytes(row["content"])
    if len(raw) > byte_limit:
        return {"error": "This document needs a smaller stored extract."}
    try:
        if row["content_encoding"] == "gzip":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                raw = stream.read(byte_limit + 1)
    except (OSError, EOFError):
        return {"error": "The stored source needs a fresh copy."}
    if len(raw) > byte_limit:
        return {"error": "This document needs a smaller stored extract."}
    if hashlib.sha256(raw).hexdigest() != reference["content_hash"]:
        return {"error": "This source copy needs its version checked."}
    if "pdf" in str(row["content_type"]).lower():
        return {"error": "This PDF needs a stored text extract."}
    text = raw.decode("utf-8", errors="replace")
    content_type = str(row["content_type"]).lower()
    if any(kind in content_type for kind in ("html", "xml")) or re.search(
        r"<(?:html|document)\b", text[:10000], re.IGNORECASE
    ):
        parser = _Text()
        parser.feed(text)
        text = "".join(parser.parts)
    lines = [re.sub(r"[\t \r]+", " ", line).strip() for line in text.splitlines()]
    paragraphs = [line for line in lines if line]
    # Split long paragraphs at word boundaries; the returned offsets expose partial passages.
    chunks = []
    for paragraph in paragraphs:
        offset = 0
        while offset < len(paragraph):
            end = min(len(paragraph), offset + MAX_PASSAGE_CHARS)
            if end < len(paragraph):
                boundary = paragraph.rfind(" ", offset, end)
                if boundary > offset:
                    end = boundary
            chunks.append(paragraph[offset:end])
            offset = end
            while offset < len(paragraph) and paragraph[offset].isspace():
                offset += 1
    terms = set(re.findall(r"[a-z0-9]{3,}", str(question).lower()[:300])) - {
        "the",
        "and",
        "for",
        "what",
        "when",
        "does",
        "this",
        "that",
        "with",
        "from",
        "are",
    }
    scores = [len(terms & set(re.findall(r"[a-z0-9]{3,}", chunk.lower()))) for chunk in chunks]
    if not chunks:
        return {"error": "This source version has no readable text."}
    anchor = max(range(len(chunks)), key=scores.__getitem__)
    # Include neighboring text so qualifications stay beside the selected terms.
    chosen = range(max(0, anchor - 1), min(len(chunks), max(0, anchor - 1) + 3))
    passages = [{"passage": i + 1, "text": chunks[i]} for i in chosen]
    return {
        **reference,
        "checked_at": datetime.now(UTC).isoformat(),
        "passages": passages,
        "total_passages": len(chunks),
        "query_matched": any(scores),
        "partial": len(chunks) > len(passages),
        "citation": "[source:" + reference["evidence_id"] + "]",
    }


def render_citations(text: str, evidence: dict[str, Any]) -> tuple[str, list[str]]:
    missing = []

    def replace(match):
        identity = match[1]
        source = evidence.get("source:" + identity, {})
        if not source.get("passages"):
            missing.append(identity)
            return match[0]
        date = str(source.get("published_at") or source.get("event_at") or "")[:10]
        return f"({source.get('source', 'source')} {date}: {source['source_url']})"

    return re.sub(r"\[source:([a-zA-Z0-9_-]+)\]", replace, text), missing
