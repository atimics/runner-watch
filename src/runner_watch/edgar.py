from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from runner_watch.ingestion import SourceFetch, SourceFetchRecorder

SEC_BASE = "https://www.sec.gov"
COMPANY_MAP_URL = f"{SEC_BASE}/files/company_tickers_exchange.json"
LATEST_FILINGS_URL = (
    f"{SEC_BASE}/cgi-bin/browse-edgar?"
    "action=getcurrent&count=100&output=atom&owner=include"
)
SEC_USER_AGENT = os.getenv(
    "SEC_USER_AGENT", "RunnerWatch/0.2 https://stonks.rati.foundation"
)
ATOM = {"atom": "http://www.w3.org/2005/Atom"}
ACCESSION_RE = re.compile(r"accession-number=([0-9-]+)")
TITLE_CIK_RE = re.compile(r"\((\d{6,10})\)\s+\((Issuer|Filer|Subject|Reporting)\)")


@dataclass(frozen=True, slots=True)
class EdgarCompany:
    cik: int
    name: str
    ticker: str
    exchange: str


@dataclass(frozen=True, slots=True)
class EdgarFiling:
    accession: str
    cik: int
    form: str
    title: str
    role: str
    filed_at: str
    filing_url: str


@dataclass(frozen=True, slots=True)
class OwnershipSummary:
    issuer_cik: int
    ticker: str
    owner_name: str
    owner_title: str
    codes: tuple[str, ...]
    purchase_shares: float
    purchase_value: float
    average_purchase_price: float | None
    sale_shares: float
    sale_value: float
    average_sale_price: float | None
    post_transaction_shares: float | None = None
    stake_change_pct: float | None = None
    is_10b5_1: bool = False
    direct_ownership: bool | None = None
    footnotes: str = ""


@dataclass(frozen=True, slots=True)
class BeneficialOwnershipSummary:
    owner_names: tuple[str, ...]
    ownership_pct: float | None
    beneficial_shares: float | None
    reporting_person_types: tuple[str, ...]


class EdgarClient:
    """Small, rate-limited client for public SEC EDGAR data."""

    def __init__(
        self,
        user_agent: str = SEC_USER_AGENT,
        max_requests_per_second: float = 6,
        recorder: Callable[[str, bytes], None] | None = None,
        fetch_recorder: SourceFetchRecorder | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.minimum_interval = 1 / max_requests_per_second
        self._last_request = 0.0
        self.recorder = recorder
        self.fetch_recorder = fetch_recorder

    @staticmethod
    def _feed_for_url(url: str) -> str:
        if url == COMPANY_MAP_URL:
            return "company_map"
        if url == LATEST_FILINGS_URL:
            return "current_filings"
        if url.endswith("/index.json"):
            return "filing_index"
        if url.lower().endswith(".xml"):
            return "filing_document"
        return "document"

    def _record_fetch(self, fetch: SourceFetch) -> None:
        if self.fetch_recorder is None:
            return
        try:
            self.fetch_recorder(fetch)
        except Exception:
            # Collection is best effort and must not block the SEC listener.
            pass

    def _get(self, url: str) -> bytes:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"www.sec.gov", "data.sec.gov"}:
            raise ValueError("Refusing a non-SEC URL")
        request = urllib.request.Request(
            url,
            headers={"User-Agent": self.user_agent, "Accept": "application/json, application/xml"},
        )
        started_at = datetime.now(UTC)
        for attempt in range(3):
            wait = self.minimum_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            try:
                with urllib.request.urlopen(request, timeout=35) as response:  # noqa: S310
                    body = response.read()
                self._last_request = time.monotonic()
                stripped = body.lstrip()
                content_type = (
                    "application/json"
                    if stripped.startswith((b"{", b"["))
                    else "application/xml"
                    if stripped.startswith(b"<")
                    else "application/octet-stream"
                )
                self._record_fetch(
                    SourceFetch.success(
                        source="sec",
                        feed=self._feed_for_url(url),
                        locator=url,
                        started_at=started_at,
                        payload=body,
                        content_type=content_type,
                        metadata={"attempts": attempt + 1},
                    )
                )
                if self.recorder:
                    try:
                        self.recorder(url, body)
                    except Exception:
                        # Archival is best effort and must not block the SEC listener.
                        pass
                return body
            except (TimeoutError, urllib.error.URLError) as exc:
                self._last_request = time.monotonic()
                if attempt == 2:
                    self._record_fetch(
                        SourceFetch.failure(
                            source="sec",
                            feed=self._feed_for_url(url),
                            locator=url,
                            started_at=started_at,
                            error=exc,
                            metadata={"attempts": 3},
                        )
                    )
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError("SEC request failed")

    def get_text(self, url: str) -> str:
        return self._get(url).decode("utf-8", errors="replace")

    def get_json(self, url: str) -> dict[str, Any]:
        return json.loads(self.get_text(url))

    def companies(self) -> list[EdgarCompany]:
        return parse_company_map(self.get_json(COMPANY_MAP_URL))

    def latest_filings(self) -> list[EdgarFiling]:
        return parse_latest_filings(self.get_text(LATEST_FILINGS_URL))

    def ownership_summary(self, filing: EdgarFiling) -> OwnershipSummary | None:
        directory_url = filing_directory_url(filing.filing_url)
        index = self.get_json(f"{directory_url}/index.json")
        items = index.get("directory", {}).get("item", [])
        xml_names = [
            str(item.get("name", ""))
            for item in items
            if str(item.get("name", "")).lower().endswith(".xml")
        ]
        for name in xml_names:
            try:
                text = self.get_text(f"{directory_url}/{name}")
                if "<ownershipDocument" in text:
                    return parse_ownership_xml(text)
            except (OSError, ValueError, ET.ParseError):
                continue
        return None

    def beneficial_ownership_summary(
        self, filing: EdgarFiling
    ) -> BeneficialOwnershipSummary | None:
        directory_url = filing_directory_url(filing.filing_url)
        index = self.get_json(f"{directory_url}/index.json")
        items = index.get("directory", {}).get("item", [])
        xml_names = [
            str(item.get("name", ""))
            for item in items
            if str(item.get("name", "")).lower().endswith(".xml")
        ]
        for name in xml_names:
            try:
                summary = parse_beneficial_ownership_xml(
                    self.get_text(f"{directory_url}/{name}")
                )
                if summary is not None:
                    return summary
            except (OSError, ValueError, ET.ParseError):
                continue
        return None


def parse_company_map(payload: dict[str, Any]) -> list[EdgarCompany]:
    fields = [str(field) for field in payload.get("fields", [])]
    positions = {field: index for index, field in enumerate(fields)}
    required = {"cik", "name", "ticker", "exchange"}
    if not required.issubset(positions):
        return []
    output: list[EdgarCompany] = []
    for row in payload.get("data", []):
        try:
            ticker = str(row[positions["ticker"]]).strip().upper()
            exchange = str(row[positions["exchange"]]).strip()
            if not ticker or exchange not in {"Nasdaq", "NYSE", "NYSE American", "Cboe"}:
                continue
            output.append(
                EdgarCompany(
                    cik=int(row[positions["cik"]]),
                    name=str(row[positions["name"]]).strip(),
                    ticker=ticker.replace(".", "-"),
                    exchange=exchange,
                )
            )
        except (IndexError, TypeError, ValueError):
            continue
    return output


def parse_latest_filings(text: str) -> list[EdgarFiling]:
    root = ET.fromstring(text)
    selected: dict[str, EdgarFiling] = {}
    role_priority = {"Issuer": 3, "Filer": 2, "Subject": 1, "Reporting": 0}
    for entry in root.findall("atom:entry", ATOM):
        title = entry.findtext("atom:title", default="", namespaces=ATOM)
        identity = entry.findtext("atom:id", default="", namespaces=ATOM)
        updated = entry.findtext("atom:updated", default="", namespaces=ATOM)
        accession_match = ACCESSION_RE.search(identity)
        cik_match = TITLE_CIK_RE.search(title)
        category = entry.find("atom:category", ATOM)
        link = entry.find("atom:link", ATOM)
        if accession_match is None or cik_match is None or category is None or link is None:
            continue
        accession = accession_match.group(1)
        role = cik_match.group(2)
        filing = EdgarFiling(
            accession=accession,
            cik=int(cik_match.group(1)),
            form=str(category.attrib.get("term", "")).upper(),
            title=title,
            role=role,
            filed_at=updated,
            filing_url=str(link.attrib.get("href", "")),
        )
        current = selected.get(accession)
        if current is None or role_priority.get(role, 0) > role_priority.get(current.role, 0):
            selected[accession] = filing
    return list(selected.values())


def filing_directory_url(filing_url: str) -> str:
    parsed = urlparse(filing_url)
    if parsed.scheme != "https" or parsed.hostname != "www.sec.gov":
        raise ValueError("Unexpected filing URL")
    if not parsed.path.startswith("/Archives/edgar/data/"):
        raise ValueError("Unexpected filing path")
    directory = parsed.path.rsplit("/", 1)[0]
    return f"{SEC_BASE}{directory}"


def _number(node: ET.Element, path: str) -> float:
    text = node.findtext(path, default="").strip()
    try:
        value = float(text)
        return value if math.isfinite(value) else 0.0
    except ValueError:
        return 0.0


def parse_ownership_xml(text: str) -> OwnershipSummary:
    root = ET.fromstring(text)
    issuer_cik = int(root.findtext("./issuer/issuerCik", default="0"))
    ticker = root.findtext("./issuer/issuerTradingSymbol", default="").strip().upper()
    owner_name = root.findtext(
        "./reportingOwner/reportingOwnerId/rptOwnerName", default=""
    ).strip()
    relationship = root.find("./reportingOwner/reportingOwnerRelationship")
    owner_title = ""
    if relationship is not None:
        owner_title = relationship.findtext("officerTitle", default="").strip()
        if not owner_title and relationship.findtext("isDirector", default="0") == "1":
            owner_title = "Director"
        if not owner_title and relationship.findtext("isTenPercentOwner", default="0") == "1":
            owner_title = "10% owner"

    codes: list[str] = []
    purchase_shares = 0.0
    purchase_value = 0.0
    sale_shares = 0.0
    sale_value = 0.0
    post_transaction_shares: float | None = None
    direct_ownership: bool | None = None
    for transaction in root.findall("./nonDerivativeTable/nonDerivativeTransaction"):
        code = transaction.findtext("./transactionCoding/transactionCode", default="").strip()
        direction = transaction.findtext(
            "./transactionAmounts/transactionAcquiredDisposedCode/value", default=""
        ).strip()
        shares = _number(transaction, "./transactionAmounts/transactionShares/value")
        price = _number(transaction, "./transactionAmounts/transactionPricePerShare/value")
        if code:
            codes.append(code)
        if code == "P" and direction == "A":
            purchase_shares += shares
            purchase_value += shares * price
        elif code == "S" and direction == "D":
            sale_shares += shares
            sale_value += shares * price
        post_shares = _number(
            transaction, "./postTransactionAmounts/sharesOwnedFollowingTransaction/value"
        )
        if post_shares > 0:
            post_transaction_shares = post_shares
        ownership_nature = transaction.findtext(
            "./ownershipNature/directOrIndirectOwnership/value", default=""
        ).strip()
        if ownership_nature in {"D", "I"}:
            direct_ownership = ownership_nature == "D"

    average_purchase_price = purchase_value / purchase_shares if purchase_shares else None
    average_sale_price = sale_value / sale_shares if sale_shares else None
    pre_transaction_shares = (
        post_transaction_shares - purchase_shares
        if post_transaction_shares is not None and purchase_shares > 0
        else None
    )
    stake_change_pct = (
        purchase_shares / pre_transaction_shares * 100
        if pre_transaction_shares is not None and pre_transaction_shares > 0
        else None
    )
    is_10b5_1 = root.findtext("./aff10b5One", default="0").strip() == "1"
    footnotes = " ".join(
        " ".join("".join(node.itertext()).split()) for node in root.findall("./footnotes/footnote")
    )[:2000]
    return OwnershipSummary(
        issuer_cik=issuer_cik,
        ticker=ticker.replace(".", "-"),
        owner_name=owner_name,
        owner_title=owner_title,
        codes=tuple(dict.fromkeys(codes)),
        purchase_shares=purchase_shares,
        purchase_value=purchase_value,
        average_purchase_price=average_purchase_price,
        sale_shares=sale_shares,
        sale_value=sale_value,
        average_sale_price=average_sale_price,
        post_transaction_shares=post_transaction_shares,
        stake_change_pct=stake_change_pct,
        is_10b5_1=is_10b5_1,
        direct_ownership=direct_ownership,
        footnotes=footnotes,
    )


def _local_tag(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1].lower()


def _first_number(values: list[str]) -> float | None:
    for value in values:
        match = re.search(r"-?[0-9][0-9,]*(?:\.[0-9]+)?", value)
        if not match:
            continue
        try:
            number = float(match.group(0).replace(",", ""))
        except ValueError:
            continue
        if math.isfinite(number):
            return number
    return None


def parse_beneficial_ownership_xml(text: str) -> BeneficialOwnershipSummary | None:
    """Read common structured Schedule 13D/G ownership fields.

    SEC schedule schemas have changed over time, so matching uses local tag
    names and accepts the common variants instead of assuming one namespace.
    """

    root = ET.fromstring(text)
    values: dict[str, list[str]] = {}
    for node in root.iter():
        clean = " ".join("".join(node.itertext()).split()) if len(node) == 0 else ""
        if clean:
            values.setdefault(_local_tag(node), []).append(clean)

    owner_tags = {
        "nameofreportingperson",
        "reportingpersonname",
        "nameofreportingpersonfiling",
    }
    percent_tags = {
        "percentofclassrepresentedbyamount",
        "percentofclass",
        "percentofclassrepresented",
    }
    share_tags = {
        "aggregateamountbeneficiallyownedbyeachreportingperson",
        "amountbeneficiallyownedbyeachreportingperson",
        "aggregateamountowned",
    }
    type_tags = {"typeofreportingperson", "reportingpersontype"}
    owners = tuple(
        dict.fromkeys(
            value[:200]
            for tag in owner_tags
            for value in values.get(tag, [])
            if value
        )
    )
    percentages = [
        number
        for tag in percent_tags
        if (number := _first_number(values.get(tag, []))) is not None
    ]
    shares = [
        number
        for tag in share_tags
        if (number := _first_number(values.get(tag, []))) is not None
    ]
    person_types = tuple(
        dict.fromkeys(
            value[:40]
            for tag in type_tags
            for value in values.get(tag, [])
            if value
        )
    )
    if not owners and not percentages and not shares:
        return None
    return BeneficialOwnershipSummary(
        owner_names=owners,
        ownership_pct=max(percentages) if percentages else None,
        beneficial_shares=max(shares) if shares else None,
        reporting_person_types=person_types,
    )


def classify_filing(form: str, ownership: OwnershipSummary | None = None) -> dict[str, Any]:
    normalized = form.upper()
    if normalized.startswith("4") and ownership:
        if ownership.purchase_value > 0:
            value = ownership.purchase_value
            score = 42
            if value >= 25_000:
                score = 50
            if value >= 100_000:
                score = 58
            if value >= 250_000:
                score = 64
            if value >= 1_000_000:
                score = 70
            return {
                "kind": "Insider purchase · check stake and financing context",
                "sentiment": "neutral",
                "score": score,
            }
        if ownership.sale_value > 0:
            return {"kind": "Insider sale · check context", "sentiment": "risk", "score": 42}
        return {"kind": "Insider ownership update", "sentiment": "neutral", "score": 28}

    rules = (
        (("S-1", "S-3", "424B", "POS AM"), "Offering or dilution filing", "risk", 82),
        (("EFFECT",), "Registration became effective", "risk", 72),
        (("NT 10-Q", "NT 10-K"), "Late periodic report", "risk", 76),
        (("144",), "Proposed security sale", "risk", 62),
        (("SC 13D",), "Active beneficial ownership · intent needs review", "neutral", 64),
        (("SC 13G",), "Beneficial ownership update", "neutral", 58),
        (("8-K", "6-K"), "New current report", "neutral", 68),
        (("10-Q", "10-K", "20-F", "40-F"), "Financial report", "neutral", 48),
    )
    for prefixes, kind, sentiment, score in rules:
        if normalized.startswith(prefixes):
            return {"kind": kind, "sentiment": sentiment, "score": score}
    return {"kind": f"New {normalized} filing", "sentiment": "neutral", "score": 30}
