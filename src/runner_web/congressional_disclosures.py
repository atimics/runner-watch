from __future__ import annotations

import hashlib
import io
import json
import os
import re
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_watch.xml_security import read_limited, safe_xml_fromstring
from runner_web.ingestion import (
    mark_source_item,
    record_source_batch,
    record_source_fetch,
    source_item_is_terminal,
)

HOUSE_INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.ZIP"
HOUSE_PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{document_id}.pdf"
HOUSE_DISCLOSURE_PAGE = "https://disclosures-clerk.house.gov/FinancialDisclosure/ViewSearch"
USER_AGENT = "RunnerWatch/0.2 congressional-disclosure ingestion https://stonks.rati.foundation"
EASTERN = ZoneInfo("America/New_York")
PARSER_VERSION = "house-ptr.v1"
MAX_INDEX_BYTES = 10 * 1024 * 1024
MAX_INDEX_XML_BYTES = 5 * 1024 * 1024
MAX_PTR_BYTES = 20 * 1024 * 1024
MAX_PTR_PAGES = 30

Download = Callable[[str, float], tuple[bytes, str | None]]

OWNER_LABELS = {
    "": "member",
    "SP": "spouse",
    "DC": "dependent_child",
    "JT": "joint",
}
TRANSACTION_LABELS = {
    "P": "purchase",
    "S": "sale",
    "S (PARTIAL)": "partial_sale",
    "E": "exchange",
}

_TRANSACTION_HEAD = re.compile(
    r"(?<![A-Z])(?P<transaction_type>S\s+\(partial\)|[PSE])\s+"
    r"(?P<transaction_date>\d{1,2}/\d{1,2}/\d{4})\s*"
    r"(?P<notification_date>\d{1,2}/\d{1,2}/\d{4})\s*"
    r"\$(?P<amount_min>[\d,]+)\s*-",
    re.IGNORECASE,
)
_AMOUNT_RANGE = re.compile(r"\$(?P<low>[\d,]+)\s*-\s*\$(?P<high>[\d,]+)")
_ASSET_TYPE = re.compile(r"\[(?P<code>[A-Z]{2,4})\]\s*$")
_TICKER = re.compile(r"\((?P<ticker>[A-Z][A-Z0-9.\-]{0,9})\)\s*\[[A-Z]{2,4}\]\s*$")
_STATUS = re.compile(r"^F\s*S\s*:\s*(?P<status>.+)$", re.IGNORECASE)
_DESCRIPTION = re.compile(r"^D\s*:\s*(?P<description>.*)$", re.IGNORECASE)
_OPTION_KIND = re.compile(r"\b(?P<kind>call|put)\s+options?\b", re.IGNORECASE)
_OPTION_CONTRACTS = re.compile(
    r"\b(?P<count>[\d,]+)\s+(?:call|put)\s+options?\b",
    re.IGNORECASE,
)
_OPTION_STRIKE = re.compile(
    r"\bstrike price of\s*\$(?P<strike>[\d,.]+)",
    re.IGNORECASE,
)
_OPTION_EXPIRY = re.compile(
    r"\bexpiration date of\s*(?P<expiry>\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_SHARE_COUNT = re.compile(
    r"\b(?:purchased|sold)\s+(?P<count>[\d,]+)\s+shares?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class HouseFiling:
    document_id: str
    filing_type: str
    filing_year: int
    filing_date: date | None
    prefix: str
    first_name: str
    last_name: str
    suffix: str
    state_district: str

    @property
    def filer_name(self) -> str:
        return " ".join(
            value for value in (self.prefix, self.first_name, self.last_name, self.suffix) if value
        )

    @property
    def source_url(self) -> str:
        return HOUSE_PTR_URL.format(year=self.filing_year, document_id=self.document_id)


@dataclass(frozen=True, slots=True)
class HouseTrade:
    row_number: int
    owner_code: str
    owner: str
    asset_name: str
    raw_asset: str
    asset_type: str
    ticker: str | None
    transaction_code: str
    transaction_type: str
    transaction_date: date
    notification_date: date
    amount_min: int
    amount_max: int
    filing_status: str
    description: str
    option_kind: str | None = None
    option_contracts: int | None = None
    option_strike: float | None = None
    option_expiry: date | None = None
    shares: int | None = None


ParsePdf = Callable[[bytes], tuple[HouseTrade, ...]]


@dataclass(frozen=True, slots=True)
class _RowLocation:
    line_index: int
    asset_start: int
    transaction_match: re.Match[str]
    asset_text: str


def house_disclosures_enabled() -> bool:
    return os.getenv("HOUSE_DISCLOSURES_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _text(value: str | None) -> str:
    return " ".join((value or "").replace("\x00", "").split())


def _parse_date(value: str) -> date:
    for pattern in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    raise ValueError(f"Unknown House disclosure date: {value}")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_house_filing_index(
    body: bytes,
    *,
    expected_year: int | None = None,
) -> tuple[HouseFiling, ...]:

    if len(body) > MAX_INDEX_BYTES:
        raise ValueError(f"House filing index exceeds the {MAX_INDEX_BYTES}-byte limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("House filing index is not a valid ZIP archive") from exc
    with archive:
        candidates = [
            item
            for item in archive.infolist()
            if not item.is_dir() and item.filename.lower().endswith("fd.xml")
        ]
        if len(candidates) != 1:
            raise ValueError("House filing index must contain exactly one FD XML file")
        member = candidates[0]
        if member.file_size > MAX_INDEX_XML_BYTES:
            raise ValueError(f"House filing XML exceeds the {MAX_INDEX_XML_BYTES}-byte limit")
        xml_body = archive.read(member)

    root = safe_xml_fromstring(xml_body, max_bytes=MAX_INDEX_XML_BYTES)
    filings: list[HouseFiling] = []
    for element in root:
        if _local_name(element.tag) != "Member":
            continue
        fields = {_local_name(child.tag): _text(child.text) for child in element}
        document_id = fields.get("DocID", "")
        year_text = fields.get("Year", "")
        filing_type = fields.get("FilingType", "").upper()
        if (
            not document_id
            or not document_id.isdigit()
            or not year_text.isdigit()
            or not filing_type
        ):
            continue
        filing_year = int(year_text)
        if expected_year is not None and filing_year != expected_year:
            continue
        filing_date_text = fields.get("FilingDate", "")
        filings.append(
            HouseFiling(
                document_id=document_id,
                filing_type=filing_type,
                filing_year=filing_year,
                filing_date=_parse_date(filing_date_text) if filing_date_text else None,
                prefix=fields.get("Prefix", ""),
                first_name=fields.get("First", ""),
                last_name=fields.get("Last", ""),
                suffix=fields.get("Suffix", ""),
                state_district=fields.get("StateDst", "").upper(),
            )
        )
    return tuple(filings)


def extract_house_ptr_text(body: bytes) -> str:
    if len(body) > MAX_PTR_BYTES:
        raise ValueError(f"House PTR exceeds the {MAX_PTR_BYTES}-byte limit")
    try:
        reader = PdfReader(io.BytesIO(body), strict=False)
    except Exception as exc:
        raise ValueError("House PTR is not a readable PDF") from exc
    if len(reader.pages) > MAX_PTR_PAGES:
        raise ValueError(f"House PTR exceeds the {MAX_PTR_PAGES}-page limit")
    pages = [(page.extract_text() or "").replace("\x00", "") for page in reader.pages]
    text = "\n---PAGE---\n".join(pages).strip()
    if not text:
        raise ValueError("House PTR has no extractable text")
    return text


def _row_barrier(value: str) -> bool:
    lowered = value.lower()
    return bool(
        _DESCRIPTION.match(value)
        or _STATUS.match(value)
        or value == "---PAGE---"
        or lowered.startswith("filing id #")
        or lowered.startswith("id owner asset")
        or lowered.startswith("name:")
        or lowered.startswith("status:")
        or lowered.startswith("state/district:")
        or lowered.startswith("* for the complete")
        or lowered in {"type", "date notification", "date", "amount cap.", "gains >", "$200?"}
    )


def _asset_for_transaction(
    lines: list[str], line_index: int, transaction_match: re.Match[str]
) -> tuple[int, str]:
    prefix = _text(lines[line_index][: transaction_match.start()])
    if _ASSET_TYPE.search(prefix):
        return line_index, prefix

    parts: list[str] = []
    found_marker = False
    asset_start = line_index
    for index in range(line_index - 1, max(-1, line_index - 8), -1):
        value = lines[index]
        if found_marker and _row_barrier(value):
            break
        if not found_marker:
            if not _ASSET_TYPE.search(value):
                if value == "---PAGE---" or _row_barrier(value):
                    break
                continue
            found_marker = True
        if _row_barrier(value):
            break
        parts.insert(0, value)
        asset_start = index
    if not parts or not _ASSET_TYPE.search(parts[-1]):
        raise ValueError(f"House PTR transaction on line {line_index + 1} has no asset")
    return asset_start, _text(" ".join(parts))


def _row_locations(lines: list[str]) -> list[_RowLocation]:
    rows: list[_RowLocation] = []
    for index, line in enumerate(lines):
        match = _TRANSACTION_HEAD.search(line)
        if match is None:
            continue
        asset_start, asset_text = _asset_for_transaction(lines, index, match)
        rows.append(
            _RowLocation(
                line_index=index,
                asset_start=asset_start,
                transaction_match=match,
                asset_text=asset_text,
            )
        )
    return rows


def _description_and_status(lines: list[str], start: int, end: int) -> tuple[str, str]:
    filing_status = "unknown"
    description_parts: list[str] = []
    description_started = False
    for value in lines[start:end]:
        status = _STATUS.match(value)
        if status:
            filing_status = _text(status.group("status")) or "unknown"
            continue
        description = _DESCRIPTION.match(value)
        if description:
            description_started = True
            first = _text(description.group("description"))
            if first:
                description_parts.append(first)
            continue
        if description_started:
            if _row_barrier(value):
                break
            description_parts.append(value)
    return filing_status, _text(" ".join(description_parts))


def _optional_date(match: re.Match[str] | None, name: str) -> date | None:
    if match is None:
        return None
    try:
        return _parse_date(match.group(name))
    except ValueError:
        return None


def parse_house_ptr_text(text: str) -> tuple[HouseTrade, ...]:

    lines = [_text(line) for line in text.replace("\r", "\n").splitlines()]
    lines = [line for line in lines if line]
    locations = _row_locations(lines)
    trades: list[HouseTrade] = []
    for row_number, location in enumerate(locations, start=1):
        next_asset_start = (
            locations[row_number].asset_start if row_number < len(locations) else len(lines)
        )
        range_text = " ".join(lines[location.line_index : next_asset_start])
        amount = _AMOUNT_RANGE.search(range_text)
        if amount is None:
            raise ValueError(f"House PTR row {row_number} has no complete amount range")

        owner_code = ""
        raw_asset = location.asset_text
        first_token, separator, remainder = raw_asset.partition(" ")
        if first_token.upper() in OWNER_LABELS and first_token:
            owner_code = first_token.upper()
            raw_asset = remainder if separator else ""
        asset_type = _ASSET_TYPE.search(raw_asset)
        if asset_type is None:
            raise ValueError(f"House PTR row {row_number} has no asset type")
        ticker_match = _TICKER.search(raw_asset)
        ticker = ticker_match.group("ticker").upper() if ticker_match else None
        asset_name = _ASSET_TYPE.sub("", raw_asset).strip()
        if ticker:
            asset_name = re.sub(rf"\s*\({re.escape(ticker)}\)\s*$", "", asset_name).strip()

        filing_status, description = _description_and_status(
            lines,
            location.line_index + 1,
            next_asset_start,
        )
        transaction_code = _text(location.transaction_match.group("transaction_type")).upper()
        transaction_code = re.sub(r"\s+", " ", transaction_code)
        option_kind_match = _OPTION_KIND.search(description)
        contracts_match = _OPTION_CONTRACTS.search(description)
        strike_match = _OPTION_STRIKE.search(description)
        expiry_match = _OPTION_EXPIRY.search(description)
        shares_match = _SHARE_COUNT.search(description)
        trades.append(
            HouseTrade(
                row_number=row_number,
                owner_code=owner_code,
                owner=OWNER_LABELS.get(owner_code, "unknown"),
                asset_name=asset_name,
                raw_asset=raw_asset,
                asset_type=asset_type.group("code"),
                ticker=ticker,
                transaction_code=transaction_code,
                transaction_type=TRANSACTION_LABELS.get(transaction_code, "other"),
                transaction_date=_parse_date(location.transaction_match.group("transaction_date")),
                notification_date=_parse_date(
                    location.transaction_match.group("notification_date")
                ),
                amount_min=int(amount.group("low").replace(",", "")),
                amount_max=int(amount.group("high").replace(",", "")),
                filing_status=filing_status,
                description=description,
                option_kind=(
                    option_kind_match.group("kind").lower() if option_kind_match else None
                ),
                option_contracts=(
                    int(contracts_match.group("count").replace(",", ""))
                    if contracts_match
                    else None
                ),
                option_strike=(
                    float(strike_match.group("strike").replace(",", "")) if strike_match else None
                ),
                option_expiry=_optional_date(expiry_match, "expiry"),
                shares=(
                    int(shares_match.group("count").replace(",", "")) if shares_match else None
                ),
            )
        )
    if not trades:
        raise ValueError("House PTR has no parseable transaction rows")
    return tuple(trades)


def parse_house_ptr_pdf(body: bytes) -> tuple[HouseTrade, ...]:
    return parse_house_ptr_text(extract_house_ptr_text(body))


def _filing_datetime(value: date) -> datetime:

    return datetime.combine(value, time.max, tzinfo=EASTERN).astimezone(UTC)


def _transaction_datetime(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=EASTERN).astimezone(UTC)


def _event_version(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def trades_to_market_events(
    filing: HouseFiling,
    trades: tuple[HouseTrade, ...],
) -> tuple[MarketEvent, ...]:
    if filing.filing_date is None:
        raise ValueError("House PTR index entry has no filing date")
    filed_at = _filing_datetime(filing.filing_date)
    events: list[MarketEvent] = []
    for trade in trades:
        if not trade.ticker:
            continue
        payload: dict[str, Any] = {
            "chamber": "house",
            "filer_name": filing.filer_name,
            "state_district": filing.state_district,
            "filing_id": filing.document_id,
            "filing_type": filing.filing_type,
            "filing_date": filing.filing_date.isoformat(),
            "filing_timestamp_precision": "date",
            "availability_time_known": False,
            "row_number": trade.row_number,
            "owner_code": trade.owner_code or None,
            "owner": trade.owner,
            "asset_name": trade.asset_name,
            "raw_asset": trade.raw_asset,
            "asset_type": trade.asset_type,
            "ticker_reported": trade.ticker,
            "transaction_code": trade.transaction_code,
            "transaction_type": trade.transaction_type,
            "transaction_date": trade.transaction_date.isoformat(),
            "notification_date": trade.notification_date.isoformat(),
            "amount_min": trade.amount_min,
            "amount_max": trade.amount_max,
            "amount_is_range": True,
            "filing_status": trade.filing_status,
            "description": trade.description,
            "option_kind": trade.option_kind,
            "option_contracts": trade.option_contracts,
            "option_strike": trade.option_strike,
            "option_expiry": trade.option_expiry.isoformat() if trade.option_expiry else None,
            "shares": trade.shares,
            "parser_version": PARSER_VERSION,
        }
        events.append(
            MarketEvent(
                event_id=f"{filing.document_id}:{trade.row_number}",
                version=_event_version(payload),
                ticker=trade.ticker,
                event_type="congressional_trade",
                event_at=filed_at,
                published_at=filed_at,
                effective_at=_transaction_datetime(trade.transaction_date),
                status=trade.filing_status.lower().replace(" ", "_"),
                source_url=filing.source_url,
                payload=payload,
            )
        )
    return tuple(events)


def _download(url: str, timeout: float) -> tuple[bytes, str | None]:
    accept = "application/pdf" if url.lower().endswith(".pdf") else "application/zip"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    )
    maximum = MAX_PTR_BYTES if url.lower().endswith(".pdf") else MAX_INDEX_BYTES
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return read_limited(response, max_bytes=maximum), response.headers.get_content_type()


def _record_failed_fetch(
    *,
    feed: str,
    locator: str,
    started_at: datetime,
    error: Exception,
    metadata: dict[str, Any],
) -> str:
    return record_source_fetch(
        SourceFetch.failure(
            source="house_clerk",
            feed=feed,
            locator=locator,
            started_at=started_at,
            error=error,
            metadata=metadata,
        )
    )


def refresh_house_disclosures(
    *,
    timeout: float = 20,
    download: Download = _download,
    parse_pdf: ParsePdf = parse_house_ptr_pdf,
    now: datetime | None = None,
    lookback_days: int | None = None,
    max_filings: int | None = None,
) -> dict[str, Any]:

    as_of = now or datetime.now(UTC)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=UTC)
    local_today = as_of.astimezone(EASTERN).date()
    days = lookback_days or _env_int(
        "HOUSE_DISCLOSURES_LOOKBACK_DAYS",
        14,
        minimum=1,
        maximum=366,
    )
    filing_limit = max_filings or _env_int(
        "HOUSE_DISCLOSURES_MAX_FILINGS_PER_RUN",
        50,
        minimum=1,
        maximum=500,
    )
    cutoff = local_today - timedelta(days=days)
    years = range(cutoff.year, local_today.year + 1)
    candidates: dict[str, HouseFiling] = {}
    index_runs: list[str] = []
    errors: list[str] = []

    for year in years:
        locator = HOUSE_INDEX_URL.format(year=year)
        started_at = datetime.now(UTC)
        try:
            body, content_type = download(locator, timeout)
        except Exception as exc:
            run_id = _record_failed_fetch(
                feed="house_filing_index",
                locator=locator,
                started_at=started_at,
                error=exc,
                metadata={"requested_count": 1, "year": year},
            )
            index_runs.append(run_id)
            errors.append(f"{year} index fetch: {exc}")
            continue
        try:
            filings = parse_house_filing_index(body, expected_year=year)
        except Exception as exc:
            fetch = SourceFetch.success(
                source="house_clerk",
                feed="house_filing_index",
                locator=locator,
                started_at=started_at,
                payload=body,
                content_type=content_type or "application/zip",
                metadata={
                    "requested_count": 1,
                    "received_count": 0,
                    "year": year,
                    "parse_error": str(exc)[:500],
                },
                partial=True,
            )
            index_runs.append(record_source_batch(SourceBatch(fetch=fetch)))
            errors.append(f"{year} index parse: {exc}")
            continue

        ptr_filings = [
            filing
            for filing in filings
            if filing.filing_type == "P"
            and filing.filing_date is not None
            and cutoff <= filing.filing_date <= local_today
        ]
        fetch = SourceFetch.success(
            source="house_clerk",
            feed="house_filing_index",
            locator=locator,
            started_at=started_at,
            payload=body,
            content_type=content_type or "application/zip",
            metadata={
                "requested_count": 1,
                "received_count": len(filings),
                "ptr_count": len(ptr_filings),
                "year": year,
            },
        )
        index_runs.append(record_source_batch(SourceBatch(fetch=fetch)))
        for filing in ptr_filings:
            candidates[filing.document_id] = filing

    ordered = sorted(
        candidates.values(),
        key=lambda filing: (filing.filing_date or date.min, filing.document_id),
        reverse=True,
    )
    downloaded = 0
    skipped = 0
    normalized_events = 0
    parsed_rows = 0
    ignored_rows = 0
    ptr_runs: list[str] = []
    for filing in ordered:
        item_key = f"{filing.document_id}:{PARSER_VERSION}"
        if source_item_is_terminal("house_clerk", "house_ptr", item_key):
            skipped += 1
            continue
        if downloaded >= filing_limit:
            break
        downloaded += 1
        started_at = datetime.now(UTC)
        try:
            body, content_type = download(filing.source_url, timeout)
        except Exception as exc:
            run_id = _record_failed_fetch(
                feed="house_ptr",
                locator=filing.source_url,
                started_at=started_at,
                error=exc,
                metadata={
                    "requested_count": 1,
                    "document_id": filing.document_id,
                    "filer_name": filing.filer_name,
                },
            )
            ptr_runs.append(run_id)
            mark_source_item(
                source="house_clerk",
                feed="house_ptr",
                item_key=item_key,
                status="pending",
                payload={"document_id": filing.document_id},
                error=str(exc),
                parser_version=PARSER_VERSION,
            )
            errors.append(f"PTR {filing.document_id} fetch: {exc}")
            continue
        try:
            trades = parse_pdf(body)
            events = trades_to_market_events(filing, trades)
        except Exception as exc:
            fetch = SourceFetch.success(
                source="house_clerk",
                feed="house_ptr",
                locator=filing.source_url,
                started_at=started_at,
                payload=body,
                content_type=content_type or "application/pdf",
                metadata={
                    "requested_count": 1,
                    "received_count": 0,
                    "document_id": filing.document_id,
                    "filer_name": filing.filer_name,
                    "parse_error": str(exc)[:500],
                    "parser_version": PARSER_VERSION,
                },
                partial=True,
            )
            ptr_runs.append(record_source_batch(SourceBatch(fetch=fetch)))
            mark_source_item(
                source="house_clerk",
                feed="house_ptr",
                item_key=item_key,
                status="rejected",
                payload={"document_id": filing.document_id},
                error=str(exc),
                parser_version=PARSER_VERSION,
            )
            errors.append(f"PTR {filing.document_id} parse: {exc}")
            continue

        fetch = SourceFetch.success(
            source="house_clerk",
            feed="house_ptr",
            locator=filing.source_url,
            started_at=started_at,
            payload=body,
            content_type=content_type or "application/pdf",
            metadata={
                "requested_count": 1,
                "received_count": len(events),
                "parsed_rows": len(trades),
                "ignored_rows_without_ticker": len(trades) - len(events),
                "document_id": filing.document_id,
                "filer_name": filing.filer_name,
                "parser_version": PARSER_VERSION,
            },
        )
        ptr_runs.append(record_source_batch(SourceBatch(fetch=fetch, market_events=events)))
        parsed_rows += len(trades)
        normalized_events += len(events)
        ignored_rows += len(trades) - len(events)
        mark_source_item(
            source="house_clerk",
            feed="house_ptr",
            item_key=item_key,
            status="processed",
            payload={
                "document_id": filing.document_id,
                "rows": len(trades),
                "events": len(events),
            },
            parser_version=PARSER_VERSION,
        )

    return {
        "status": "partial" if errors else "success",
        "index_runs": index_runs,
        "ptr_runs": ptr_runs,
        "filings_considered": len(ordered),
        "filings_downloaded": downloaded,
        "filings_skipped": skipped,
        "parsed_rows": parsed_rows,
        "events": normalized_events,
        "ignored_rows_without_ticker": ignored_rows,
        "errors": errors,
    }
