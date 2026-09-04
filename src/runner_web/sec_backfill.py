from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from runner_watch.edgar import (
    BeneficialOwnershipSummary,
    OwnershipSummary,
    classify_filing,
    parse_beneficial_ownership_xml,
    parse_ownership_xml,
)
from runner_watch.ingestion import SourceFetch
from runner_web import db
from runner_web.ingestion import mark_source_item, record_source_fetch
from runner_web.sec_facts import refresh_company_facts

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_BASE = "https://data.sec.gov/submissions/"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/"
DEFAULT_FORMS = (
    "4",
    "8-K",
    "6-K",
    "S-1",
    "S-3",
    "424B",
    "EFFECT",
    "144",
    "SC 13D",
    "SC 13G",
    "NT 10-Q",
    "NT 10-K",
    "10-Q",
    "10-K",
    "20-F",
    "40-F",
)
BACKFILL_PARSER_VERSION = "sec-backfill-v1"

Download = Callable[[str, float], tuple[bytes, str | None]]


@dataclass(frozen=True, slots=True)
class SubmissionFiling:
    accession: str
    cik: int
    company: str
    ticker: str
    form: str
    filed_at: str
    primary_document: str
    filing_url: str


@dataclass(slots=True)
class BackfillResult:
    issuers_selected: int = 0
    issuers_completed: int = 0
    issuers_skipped: int = 0
    submission_files_fetched: int = 0
    archived_responses_reused: int = 0
    filings_selected: int = 0
    filings_inserted: int = 0
    filings_skipped: int = 0
    documents_fetched: int = 0
    facts_loaded: int = 0
    facts_unavailable: int = 0
    errors: int = 0


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _http_error_status(error: BaseException) -> int | None:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError):
            return int(current.code)
        current = current.__cause__ or current.__context__
    return None


def _informative_user_agent(value: str) -> bool:
    clean = value.strip()
    has_contact = "@" in clean or "http://" in clean or "https://" in clean
    return bool(clean and " " in clean and has_contact)


class SecHttpClient:
    def __init__(
        self,
        user_agent: str,
        *,
        requests_per_second: float = 2.0,
        attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not _informative_user_agent(user_agent):
            raise ValueError(
                "SEC_USER_AGENT must identify the application and contact URL or email"
            )
        if not 0 < requests_per_second <= 10:
            raise ValueError("requests_per_second must be greater than zero and at most 10")
        if attempts < 1:
            raise ValueError("attempts must be positive")
        self.user_agent = user_agent
        self.minimum_interval = 1 / requests_per_second
        self.attempts = attempts
        self.sleep = sleep
        self.monotonic = monotonic
        self.last_request = 0.0

    def __call__(self, url: str, timeout: float) -> tuple[bytes, str | None]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"www.sec.gov", "data.sec.gov"}:
            raise ValueError("refusing a non-SEC URL")
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json,text/html,application/xml,text/plain",
            },
        )
        for attempt in range(self.attempts):
            wait = self.minimum_interval - (self.monotonic() - self.last_request)
            if wait > 0:
                self.sleep(wait)
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    body = response.read()
                    content_type = response.headers.get_content_type()
                self.last_request = self.monotonic()
                return body, content_type
            except urllib.error.HTTPError as exc:
                self.last_request = self.monotonic()
                if exc.code not in {403, 429, 500, 502, 503, 504} or attempt + 1 >= self.attempts:
                    raise
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    delay = float(retry_after) if retry_after else float(2**attempt)
                except ValueError:
                    delay = float(2**attempt)
                self.sleep(min(60.0, max(1.0, delay)))
            except (TimeoutError, urllib.error.URLError):
                self.last_request = self.monotonic()
                if attempt + 1 >= self.attempts:
                    raise
                self.sleep(float(2**attempt))
        raise RuntimeError("SEC request failed")


def _content_type(url: str, supplied: str | None, body: bytes) -> str:
    if supplied:
        return supplied
    if url.endswith(".json") or body.lstrip().startswith((b"{", b"[")):
        return "application/json"
    if url.endswith((".htm", ".html")):
        return "text/html"
    if url.endswith(".xml") or body.lstrip().startswith(b"<"):
        return "application/xml"
    return "text/plain"


def _fetch_and_archive(
    download: Download,
    url: str,
    *,
    feed: str,
    timeout: float,
    metadata: dict[str, Any],
) -> bytes:
    started_at = datetime.now(UTC)
    try:
        body, supplied_type = download(url, timeout)
    except Exception as exc:
        record_source_fetch(
            SourceFetch.failure(
                source="sec",
                feed=feed,
                locator=url,
                started_at=started_at,
                error=exc,
                metadata=metadata,
            )
        )
        raise
    record_source_fetch(
        SourceFetch.success(
            source="sec",
            feed=feed,
            locator=url,
            started_at=started_at,
            payload=body,
            content_type=_content_type(url, supplied_type, body),
            metadata=metadata,
        )
    )
    return body


def _columnar_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    recent = payload.get("filings", {}).get("recent")
    table = recent if isinstance(recent, dict) else payload
    if not isinstance(table, dict):
        return []
    accessions = table.get("accessionNumber")
    if not isinstance(accessions, list):
        return []
    rows: list[dict[str, Any]] = []
    for index, accession in enumerate(accessions):
        row: dict[str, Any] = {}
        for key, values in table.items():
            if isinstance(values, list) and index < len(values):
                row[key] = values[index]
        row["accessionNumber"] = accession
        rows.append(row)
    return rows


def _filing_url(cik: int, accession: str, primary_document: str) -> str:
    compact = accession.replace("-", "")
    filename = primary_document.strip() or f"{accession}.txt"
    if "/" in filename or filename in {".", ".."}:
        raise ValueError("SEC primary document name is unsafe")
    return f"{ARCHIVES_BASE}{cik}/{compact}/{filename}"


def parse_submission_filings(
    payloads: list[dict[str, Any]],
    *,
    cik: int,
    company: str,
    ticker: str,
    start_date: date,
    end_date: date,
    forms: tuple[str, ...] = DEFAULT_FORMS,
) -> list[SubmissionFiling]:
    selected: dict[str, SubmissionFiling] = {}
    for payload in payloads:
        for row in _columnar_rows(payload):
            accession = str(row.get("accessionNumber") or "").strip()
            form = str(row.get("form") or "").strip().upper()
            filing_day_text = str(row.get("filingDate") or "").strip()
            try:
                filing_day = date.fromisoformat(filing_day_text)
            except ValueError:
                continue
            if (
                not accession
                or not form.startswith(forms)
                or filing_day < start_date
                or filing_day > end_date
            ):
                continue
            acceptance = str(row.get("acceptanceDateTime") or "").strip()
            filed_at = f"{filing_day_text}T00:00:00+00:00"
            if acceptance:
                try:
                    parsed = datetime.fromisoformat(acceptance.replace("Z", "+00:00"))
                except ValueError:
                    try:
                        parsed = datetime.strptime(acceptance, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
                    except ValueError:
                        parsed = None
                if parsed is not None:
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    filed_at = parsed.astimezone(UTC).isoformat()
            primary_document = str(row.get("primaryDocument") or "").strip()
            try:
                filing_url = _filing_url(cik, accession, primary_document)
            except ValueError:
                continue
            selected[accession] = SubmissionFiling(
                accession=accession,
                cik=cik,
                company=company,
                ticker=ticker,
                form=form,
                filed_at=filed_at,
                primary_document=primary_document,
                filing_url=filing_url,
            )
    return sorted(selected.values(), key=lambda filing: (filing.filed_at, filing.accession))


def _historical_file_names(payload: dict[str, Any], start_date: date) -> list[str]:
    files = payload.get("filings", {}).get("files")
    if not isinstance(files, list):
        return []
    names: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or "/" in name:
            continue
        filing_to = str(item.get("filingTo") or "")
        try:
            if filing_to and date.fromisoformat(filing_to) < start_date:
                continue
        except ValueError:
            pass
        names.append(name)
    return sorted(set(names))


def _balanced_recent_filings(
    filings: list[SubmissionFiling], limit: int | None
) -> list[SubmissionFiling]:
    if limit is None or len(filings) <= limit:
        return filings
    groups: dict[str, list[SubmissionFiling]] = {}
    for filing in reversed(filings):
        bucket = filing.form.split("/", 1)[0]
        groups.setdefault(bucket, []).append(filing)
    selected: list[SubmissionFiling] = []
    while len(selected) < limit and groups:
        for bucket in sorted(tuple(groups)):
            selected.append(groups[bucket].pop(0))
            if not groups[bucket]:
                del groups[bucket]
            if len(selected) >= limit:
                break
    return sorted(selected, key=lambda filing: (filing.filed_at, filing.accession))


def _document_already_archived(url: str) -> bool:
    with db.connection() as database:
        return (
            database.execute(
                "SELECT 1 FROM source_documents WHERE source='sec' AND source_url=? LIMIT 1",
                (url,),
            ).fetchone()
            is not None
        )


def _archived_body(url: str) -> bytes | None:
    with db.connection() as database:
        row = database.execute(
            """
            SELECT content,content_encoding,content_hash FROM source_documents
            WHERE source='sec' AND source_url=?
            ORDER BY last_collected_at DESC,content_hash DESC LIMIT 1
            """,
            (url,),
        ).fetchone()
    if row is None:
        return None
    body = bytes(row["content"])
    if row["content_encoding"] == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            return None
    if hashlib.sha256(body).hexdigest() != str(row["content_hash"]):
        return None
    return body


def _fetch_or_reuse(
    download: Download,
    url: str,
    *,
    feed: str,
    timeout: float,
    metadata: dict[str, Any],
) -> tuple[bytes, bool]:
    archived = _archived_body(url)
    if archived is not None:
        return archived, True
    return (
        _fetch_and_archive(
            download,
            url,
            feed=feed,
            timeout=timeout,
            metadata=metadata,
        ),
        False,
    )


def _filing_already_stored(accession: str) -> bool:
    with db.connection() as database:
        return (
            database.execute("SELECT 1 FROM sec_filings WHERE accession=?", (accession,)).fetchone()
            is not None
        )


def _backfill_state(state_key: str) -> str | None:
    with db.connection() as database:
        row = database.execute(
            """
            SELECT status FROM source_item_state
            WHERE source='sec' AND feed='training_backfill' AND item_key=?
            """,
            (state_key,),
        ).fetchone()
    return str(row["status"]) if row else None


def _parse_document(
    filing: SubmissionFiling, body: bytes
) -> tuple[OwnershipSummary | None, BeneficialOwnershipSummary | None]:
    text = body.decode("utf-8", errors="replace")
    ownership: OwnershipSummary | None = None
    beneficial: BeneficialOwnershipSummary | None = None
    try:
        if filing.form.startswith("4") and "ownershipDocument" in text:
            ownership = parse_ownership_xml(text)
        elif filing.form.startswith(("SC 13D", "SC 13G")):
            beneficial = parse_beneficial_ownership_xml(text)
    except Exception:
        pass
    return ownership, beneficial


def _insert_filing(
    filing: SubmissionFiling,
    ownership: OwnershipSummary | None,
    beneficial: BeneficialOwnershipSummary | None,
) -> bool:
    classification = classify_filing(filing.form, ownership)
    is_purchase = bool(ownership and ownership.purchase_value)
    timestamp = datetime.now(UTC).isoformat()
    with db.connection() as database:
        company = database.execute(
            """
            SELECT ticker,name FROM sec_companies WHERE cik=?
            ORDER BY ticker LIMIT 1
            """,
            (ownership.issuer_cik if ownership else filing.cik,),
        ).fetchone()
        ticker = str(company["ticker"]) if company else filing.ticker
        company_name = str(company["name"]) if company else filing.company
        cursor = database.execute(
            """
            INSERT OR IGNORE INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,actor,actor_title,transaction_codes,transaction_shares,
                transaction_price,transaction_value,created_at,updated_at,parser_version,
                post_transaction_shares,stake_change_pct,is_10b5_1,direct_ownership,footnotes,
                beneficial_ownership_pct,beneficial_shares,beneficial_owner_names,
                reporting_person_types
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                filing.accession,
                ownership.issuer_cik if ownership else filing.cik,
                ownership.ticker if ownership and ownership.ticker else ticker,
                company_name,
                filing.form,
                classification["kind"],
                classification["sentiment"],
                float(classification["score"]),
                f"{filing.form} filing for {company_name}",
                filing.filed_at,
                filing.filing_url,
                ownership.owner_name if ownership else None,
                ownership.owner_title if ownership else None,
                ",".join(ownership.codes) if ownership else "",
                (ownership.purchase_shares if is_purchase else ownership.sale_shares)
                if ownership
                else None,
                (ownership.average_purchase_price if is_purchase else ownership.average_sale_price)
                if ownership
                else None,
                (ownership.purchase_value if is_purchase else ownership.sale_value)
                if ownership
                else None,
                timestamp,
                timestamp,
                BACKFILL_PARSER_VERSION,
                ownership.post_transaction_shares if ownership else None,
                ownership.stake_change_pct if ownership else None,
                int(ownership.is_10b5_1) if ownership else 0,
                int(ownership.direct_ownership)
                if ownership and ownership.direct_ownership is not None
                else None,
                ownership.footnotes if ownership else "",
                beneficial.ownership_pct if beneficial else None,
                beneficial.beneficial_shares if beneficial else None,
                ",".join(beneficial.owner_names) if beneficial else "",
                ",".join(beneficial.reporting_person_types) if beneficial else "",
            ),
        )
    return cursor.rowcount > 0


def runner_issuer_universe(*, issuer_limit: int | None = None) -> list[dict[str, Any]]:
    with db.connection() as database:
        rows = database.execute(
            """
            SELECT c.cik,MIN(c.ticker) AS ticker,MIN(c.name) AS name
            FROM sec_companies c
            JOIN (SELECT DISTINCT UPPER(ticker) AS ticker FROM scan_snapshots) s
              ON s.ticker=UPPER(c.ticker)
            GROUP BY c.cik
            ORDER BY c.cik
            """
        ).fetchall()
    issuers = [dict(row) for row in rows]
    return issuers[:issuer_limit] if issuer_limit is not None else issuers


def _selected_issuers(ciks: tuple[int, ...], issuer_limit: int | None) -> list[dict[str, Any]]:
    if not ciks:
        return runner_issuer_universe(issuer_limit=issuer_limit)
    placeholders = ",".join("?" for _ in ciks)
    with db.connection() as database:
        rows = database.execute(
            f"""
            SELECT cik,MIN(ticker) AS ticker,MIN(name) AS name
            FROM sec_companies WHERE cik IN ({placeholders})
            GROUP BY cik ORDER BY cik
            """,
            ciks,
        ).fetchall()
    found = {int(row["cik"]): dict(row) for row in rows}
    missing = sorted(set(ciks) - set(found))
    if missing:
        raise ValueError(f"CIKs are missing from sec_companies: {', '.join(map(str, missing))}")
    issuers = [found[cik] for cik in sorted(set(ciks))]
    return issuers[:issuer_limit] if issuer_limit is not None else issuers


def backfill_sec_corpus(
    *,
    start_date: date,
    end_date: date,
    download: Download,
    ciks: tuple[int, ...] = (),
    forms: tuple[str, ...] = DEFAULT_FORMS,
    issuer_limit: int | None = None,
    max_filings_per_issuer: int | None = None,
    max_documents: int | None = None,
    include_company_facts: bool = True,
    timeout: float = 35,
) -> BackfillResult:
    if start_date > end_date:
        raise ValueError("start_date must be on or before end_date")
    if issuer_limit is not None and issuer_limit < 1:
        raise ValueError("issuer_limit must be positive")
    if max_documents is not None and max_documents < 1:
        raise ValueError("max_documents must be positive")
    if max_filings_per_issuer is not None and max_filings_per_issuer < 1:
        raise ValueError("max_filings_per_issuer must be positive")
    issuers = _selected_issuers(ciks, issuer_limit)
    result = BackfillResult(issuers_selected=len(issuers))
    for issuer in issuers:
        cik = int(issuer["cik"])
        filing_scope = max_filings_per_issuer if max_filings_per_issuer is not None else "all"
        state_key = (
            f"{cik}:{start_date.isoformat()}:{end_date.isoformat()}:{','.join(forms)}:"
            f"filings={filing_scope}:facts={int(include_company_facts)}"
        )
        existing_state = _backfill_state(state_key)
        if existing_state == "processed":
            result.issuers_skipped += 1
            continue
        errors: list[str] = []
        truncated = False
        main_url = SUBMISSIONS_URL.format(cik=cik)
        try:
            if existing_state is None:
                main_body = _fetch_and_archive(
                    download,
                    main_url,
                    feed="submissions",
                    timeout=timeout,
                    metadata={"cik": cik, "kind": "recent"},
                )
                main_reused = False
            else:
                main_body, main_reused = _fetch_or_reuse(
                    download,
                    main_url,
                    feed="submissions",
                    timeout=timeout,
                    metadata={"cik": cik, "kind": "recent"},
                )
            result.archived_responses_reused += int(main_reused)
            main_payload = json.loads(main_body)
            if not isinstance(main_payload, dict):
                raise ValueError("SEC submissions response is not an object")
            payloads = [main_payload]
            result.submission_files_fetched += int(not main_reused)
            for name in _historical_file_names(main_payload, start_date):
                history_url = urljoin(SUBMISSIONS_BASE, name)
                history_body, history_reused = _fetch_or_reuse(
                    download,
                    history_url,
                    feed="submissions_history",
                    timeout=timeout,
                    metadata={"cik": cik, "kind": "history", "name": name},
                )
                result.archived_responses_reused += int(history_reused)
                history_payload = json.loads(history_body)
                if isinstance(history_payload, dict):
                    payloads.append(history_payload)
                    result.submission_files_fetched += int(not history_reused)
            filings = parse_submission_filings(
                payloads,
                cik=cik,
                company=str(issuer["name"]),
                ticker=str(issuer["ticker"]),
                start_date=start_date,
                end_date=end_date,
                forms=forms,
            )
            filings = _balanced_recent_filings(filings, max_filings_per_issuer)
            result.filings_selected += len(filings)
            for filing in filings:
                if max_documents is not None and result.documents_fetched >= max_documents:
                    truncated = True
                    break
                if _filing_already_stored(filing.accession) and _document_already_archived(
                    filing.filing_url
                ):
                    result.filings_skipped += 1
                    continue
                try:
                    body = _archived_body(filing.filing_url)
                    if body is None:
                        body = _fetch_and_archive(
                            download,
                            filing.filing_url,
                            feed="filing_document",
                            timeout=timeout,
                            metadata={
                                "cik": cik,
                                "accession": filing.accession,
                                "form": filing.form,
                            },
                        )
                        result.documents_fetched += 1
                    else:
                        result.archived_responses_reused += 1
                    ownership, beneficial = _parse_document(filing, body)
                    if _insert_filing(filing, ownership, beneficial):
                        result.filings_inserted += 1
                    else:
                        result.filings_skipped += 1
                    mark_source_item(
                        source="sec",
                        feed="training_filing",
                        item_key=filing.accession,
                        status="processed",
                        payload={
                            "cik": cik,
                            "form": filing.form,
                            "filed_at": filing.filed_at,
                            "filing_url": filing.filing_url,
                        },
                        parser_version=BACKFILL_PARSER_VERSION,
                    )
                except Exception as exc:
                    result.errors += 1
                    errors.append(f"{filing.accession}: {exc}")
                    mark_source_item(
                        source="sec",
                        feed="training_filing",
                        item_key=filing.accession,
                        status="pending",
                        payload={"cik": cik, "filing_url": filing.filing_url},
                        error=str(exc),
                        parser_version=BACKFILL_PARSER_VERSION,
                    )
            if include_company_facts and (
                max_documents is None or result.documents_fetched < max_documents
            ):
                try:
                    fact_result = refresh_company_facts(cik, timeout=timeout, download=download)
                    result.facts_loaded += int(fact_result["facts"])
                except Exception as exc:
                    if _http_error_status(exc) == 404:
                        result.facts_unavailable += 1
                    else:
                        result.errors += 1
                        errors.append(f"company facts: {exc}")
        except Exception as exc:
            result.errors += 1
            errors.append(str(exc))
        status = "processed" if not errors and not truncated else "pending"
        mark_source_item(
            source="sec",
            feed="training_backfill",
            item_key=state_key,
            status=status,
            payload={
                "cik": cik,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            error=(
                "; ".join(errors)
                or ("bounded run stopped before the issuer was complete" if truncated else None)
            ),
            parser_version=BACKFILL_PARSER_VERSION,
        )
        if not errors and not truncated:
            result.issuers_completed += 1
        if max_documents is not None and result.documents_fetched >= max_documents:
            break
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill point-in-time SEC evidence for training")
    parser.add_argument("--database-path", type=Path)
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument("--cik", action="append", type=int, default=[])
    parser.add_argument("--form", action="append", dest="forms")
    parser.add_argument("--issuer-limit", type=int)
    parser.add_argument("--max-filings-per-issuer", type=int, default=40)
    parser.add_argument("--max-documents", type=int)
    parser.add_argument("--skip-company-facts", action="store_true")
    parser.add_argument("--requests-per-second", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=35)
    parser.add_argument("--user-agent")
    arguments = parser.parse_args()
    if arguments.years < 1:
        parser.error("--years must be positive")
    if arguments.database_path:
        if db.DATABASE_URL:
            parser.error("--database-path cannot be combined with DATABASE_URL")
        db.DATABASE_PATH = arguments.database_path
    user_agent = arguments.user_agent
    if not user_agent:
        import os

        user_agent = os.getenv("SEC_USER_AGENT", "RunnerWatch/0.2 https://stonks.rati.foundation")
    end_date = arguments.end_date
    start_date = arguments.start_date or end_date - timedelta(days=arguments.years * 366)
    result = backfill_sec_corpus(
        start_date=start_date,
        end_date=end_date,
        download=SecHttpClient(
            user_agent,
            requests_per_second=arguments.requests_per_second,
        ),
        ciks=tuple(arguments.cik),
        forms=tuple(arguments.forms or DEFAULT_FORMS),
        issuer_limit=arguments.issuer_limit,
        max_filings_per_issuer=arguments.max_filings_per_issuer,
        max_documents=arguments.max_documents,
        include_company_facts=not arguments.skip_company_facts,
        timeout=arguments.timeout,
    )
    print(_canonical_json(asdict(result)))
    if result.errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
