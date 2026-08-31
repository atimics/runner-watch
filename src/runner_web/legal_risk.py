from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from runner_watch.ingestion import SourceBatch, SourceFetch
from runner_web.db import connection
from runner_web.ingestion import record_source_batch

PACER_PARTY_SEARCH_URL = "https://pcl.uscourts.gov/pcl-public-api/rest/parties/find"
PACER_CASE_LOCATOR_URL = "https://pcl.uscourts.gov/pcl/index.jsf"
OALJ_SEARCH_URL = "https://www.dol.gov/agencies/oalj/apps/keyword-search/OALJ_NEW_SEARCH"
REVIEW_STATUSES = {"pending", "approved", "rejected"}
RISK_LABELS = {"unknown", "watch", "material"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "esq"}
ORGANIZATION_WORDS = {
    "advisors",
    "capital",
    "company",
    "corp",
    "corporation",
    "foundation",
    "fund",
    "holdings",
    "inc",
    "incorporated",
    "limited",
    "llc",
    "llp",
    "lp",
    "partners",
    "plc",
    "trust",
}


@dataclass(frozen=True, slots=True)
class LegalSearchTarget:
    person_id: str
    display_name: str
    normalized_name: str
    sec_person_cik: int | None
    ticker: str
    issuer_cik: int
    filing_role: str
    link_confidence: float


@dataclass(frozen=True, slots=True)
class LegalCaseCandidate:
    source: str
    feed: str
    external_case_id: str
    person_id: str
    ticker: str
    issuer_cik: int
    case_number: str
    case_title: str
    party_name: str
    source_url: str
    name_match_confidence: float
    match_method: str
    court: str | None = None
    jurisdiction_type: str | None = None
    party_role: str | None = None
    case_type: str | None = None
    nature_of_suit: str | None = None
    filed_at: str | None = None
    closed_at: str | None = None
    case_status: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def normalize_person_name(value: str) -> str:
    """Create a comparison key without claiming two matching names are one person."""

    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    words = re.findall(r"[a-z0-9]+", normalized.casefold())
    while words and words[-1] in NAME_SUFFIXES:
        words.pop()
    return " ".join(words)


def _entity_type(name: str) -> str:
    words = set(normalize_person_name(name).split())
    return "organization_candidate" if words & ORGANIZATION_WORDS else "person_candidate"


def _person_id(name: str, issuer_cik: int, sec_person_cik: int | None) -> str:
    identity = (
        f"sec-reporting-owner:{sec_person_cik}"
        if sec_person_cik
        else f"sec-filing-name:{issuer_cik}:{normalize_person_name(name)}"
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def _clean_name(value: Any) -> str:
    return " ".join(str(value or "").split())[:240]


def _trusted_source_url(value: Any, allowed_suffix: str, fallback: str) -> str:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and (
        hostname == allowed_suffix or hostname.endswith(f".{allowed_suffix}")
    ):
        return candidate
    return fallback


def _upsert_filing_person(
    database: Any,
    *,
    name: str,
    sec_person_cik: int | None,
    entity_type: str,
    ticker: str,
    issuer_cik: int,
    accession: str,
    filing_role: str,
    confidence: float,
    filed_at: str,
) -> bool:
    normalized = normalize_person_name(name)
    if len(normalized.split()) < 2:
        return False
    person_id = _person_id(name, issuer_cik, sec_person_cik)
    timestamp = _iso()
    database.execute(
        """
        INSERT INTO filing_people(
            id,display_name,normalized_name,sec_person_cik,entity_type,
            review_status,created_at,updated_at
        ) VALUES(?,?,?,?,?,'pending',?,?)
        ON CONFLICT(id) DO UPDATE SET
            display_name=excluded.display_name,
            normalized_name=excluded.normalized_name,
            sec_person_cik=COALESCE(excluded.sec_person_cik,filing_people.sec_person_cik),
            entity_type=excluded.entity_type,
            updated_at=excluded.updated_at
        """,
        (
            person_id,
            name,
            normalized,
            sec_person_cik,
            entity_type,
            timestamp,
            timestamp,
        ),
    )
    database.execute(
        """
        INSERT INTO filing_person_issuer_links(
            person_id,ticker,issuer_cik,source_accession,filing_role,confidence,
            review_status,first_seen_at,last_seen_at
        ) VALUES(?,?,?,?,?,?,'pending',?,?)
        ON CONFLICT(person_id,ticker,source_accession) DO UPDATE SET
            filing_role=excluded.filing_role,
            confidence=excluded.confidence,
            last_seen_at=excluded.last_seen_at
        """,
        (
            person_id,
            ticker,
            issuer_cik,
            accession,
            filing_role,
            confidence,
            filed_at,
            filed_at,
        ),
    )
    return True


def sync_filing_people(limit: int = 2_000) -> dict[str, int]:
    """Stage people named in SEC ownership filings for identity review."""

    limit = max(1, min(limit, 20_000))
    with connection() as database:
        rows = database.execute(
            """
            SELECT accession,cik,ticker,form,filed_at,actor,actor_cik,actor_title,
                   beneficial_owner_names
            FROM sec_filings
            WHERE actor IS NOT NULL OR beneficial_owner_names<>''
            ORDER BY filed_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        staged = 0
        links = 0
        for row in rows:
            ticker = str(row["ticker"]).strip().upper()
            issuer_cik = int(row["cik"])
            accession = str(row["accession"])
            filed_at = str(row["filed_at"])
            actor = _clean_name(row["actor"])
            if actor:
                sec_person_cik = int(row["actor_cik"]) if row["actor_cik"] else None
                inserted = _upsert_filing_person(
                    database,
                    name=actor,
                    sec_person_cik=sec_person_cik,
                    entity_type=_entity_type(actor),
                    ticker=ticker,
                    issuer_cik=issuer_cik,
                    accession=accession,
                    filing_role=_clean_name(row["actor_title"])
                    or f"Form {row['form']} reporting owner",
                    confidence=0.99 if sec_person_cik else 0.72,
                    filed_at=filed_at,
                )
                staged += int(inserted)
                links += int(inserted)
            beneficial_names = [
                _clean_name(name)
                for name in str(row["beneficial_owner_names"] or "").split(",")
                if _clean_name(name)
            ]
            for name in beneficial_names:
                inserted = _upsert_filing_person(
                    database,
                    name=name,
                    sec_person_cik=None,
                    entity_type=_entity_type(name),
                    ticker=ticker,
                    issuer_cik=issuer_cik,
                    accession=accession,
                    filing_role=f"Form {row['form']} beneficial owner",
                    confidence=0.55,
                    filed_at=filed_at,
                )
                staged += int(inserted)
                links += int(inserted)
    return {"filings": len(rows), "people_staged": staged, "links_staged": links}


def review_filing_person(
    person_id: str,
    status: str,
    *,
    note: str = "",
) -> None:
    """Record the human identity decision required before a legal search."""

    if status not in REVIEW_STATUSES:
        raise ValueError("Unknown person review status")
    with connection() as database:
        updated = database.execute(
            "UPDATE filing_people SET review_status=?,review_note=?,updated_at=? WHERE id=?",
            (status, note[:1_000] or None, _iso(), person_id),
        )
        if updated.rowcount != 1:
            raise ValueError("Unknown filing person")


def review_filing_person_link(
    person_id: str,
    ticker: str,
    issuer_cik: int,
    status: str,
    *,
    note: str = "",
) -> None:
    """Review one person's relationship to one issuer separately from identity."""

    if status not in REVIEW_STATUSES:
        raise ValueError("Unknown issuer-link review status")
    with connection() as database:
        updated = database.execute(
            """
            UPDATE filing_person_issuer_links
            SET review_status=?,review_note=?
            WHERE person_id=? AND ticker=? AND issuer_cik=?
            """,
            (status, note[:1_000] or None, person_id, ticker.strip().upper(), issuer_cik),
        )
        if updated.rowcount < 1:
            raise ValueError("Unknown filing person-to-issuer link")


def legal_search_targets(limit: int = 25) -> list[LegalSearchTarget]:
    """Return only people and issuer links that a reviewer approved."""

    limit = max(1, min(limit, 500))
    with connection() as database:
        rows = database.execute(
            """
            SELECT p.id,p.display_name,p.normalized_name,p.sec_person_cik,
                   l.ticker,l.issuer_cik,l.filing_role,l.confidence
            FROM filing_people p
            JOIN filing_person_issuer_links l ON l.person_id=p.id
            WHERE p.review_status='approved' AND l.review_status='approved'
              AND p.entity_type='person_candidate'
            ORDER BY l.last_seen_at DESC,l.confidence DESC,p.display_name
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        LegalSearchTarget(
            person_id=str(row["id"]),
            display_name=str(row["display_name"]),
            normalized_name=str(row["normalized_name"]),
            sec_person_cik=int(row["sec_person_cik"]) if row["sec_person_cik"] else None,
            ticker=str(row["ticker"]),
            issuer_cik=int(row["issuer_cik"]),
            filing_role=str(row["filing_role"]),
            link_confidence=float(row["confidence"]),
        )
        for row in rows
    ]


def pacer_party_search_criteria(
    target: LegalSearchTarget,
    *,
    filed_from: str | None = None,
) -> dict[str, Any]:
    """Build one immediate, page-one PACER party search request."""

    words = target.normalized_name.split()
    if len(words) < 2:
        raise ValueError("PACER party searches require at least a first and last name")
    court_case: dict[str, Any] = {"jurisdictionType": "cv"}
    if filed_from:
        court_case["dateFiledFrom"] = filed_from
    criteria: dict[str, Any] = {
        "firstName": words[0],
        "lastName": words[-1],
        "courtCase": court_case,
        "requestType": "Immediate",
        "requestSource": "Other",
    }
    if len(words) > 2:
        criteria["middleName"] = " ".join(words[1:-1])
    return criteria


def person_name_match(candidate_name: str, target_name: str) -> tuple[float, str] | None:
    """Return a conservative name match that still requires later human review."""

    candidate = normalize_person_name(candidate_name)
    target = normalize_person_name(target_name)
    if not candidate or not target:
        return None
    if candidate == target:
        return 1.0, "exact_normalized_name"
    candidate_words = candidate.split()
    target_words = target.split()
    if (
        len(candidate_words) >= 2
        and len(target_words) >= 2
        and candidate_words[0] == target_words[0]
        and candidate_words[-1] == target_words[-1]
    ):
        candidate_middle = "".join(word[0] for word in candidate_words[1:-1])
        target_middle = "".join(word[0] for word in target_words[1:-1])
        if not candidate_middle or not target_middle or candidate_middle == target_middle:
            return 0.9, "first_last_middle_compatible"
    return None


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("content", "results", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            for nested_key in ("content", "results", "items"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [row for row in nested if isinstance(row, dict)]
    return []


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", []):
            return value
    return None


def parse_pacer_party_results(
    payload: dict[str, Any],
    target: LegalSearchTarget,
) -> tuple[LegalCaseCandidate, ...]:
    """Normalize PACER party results into the private review queue."""

    output: list[LegalCaseCandidate] = []
    seen: set[str] = set()
    for row in _rows(payload):
        court_case = row.get("courtCase") if isinstance(row.get("courtCase"), dict) else {}
        party_name = _clean_name(
            _first(row, "partyName", "name")
            or " ".join(
                str(row.get(key) or "")
                for key in ("firstName", "middleName", "lastName", "generation")
            )
        )
        match = person_name_match(party_name, target.display_name)
        if match is None:
            continue
        match_confidence, match_method = match
        court = str(_first(row, "courtId") or _first(court_case, "courtId") or "").strip()
        case_number = str(
            _first(row, "caseNumberFull", "caseNumber")
            or _first(court_case, "caseNumberFull", "caseNumber")
            or ""
        ).strip()
        external_case_id = str(
            _first(row, "caseId") or _first(court_case, "caseId") or f"{court}:{case_number}"
        ).strip()
        if not case_number or not external_case_id or external_case_id in seen:
            continue
        seen.add(external_case_id)
        case_title = _clean_name(
            _first(row, "caseTitle") or _first(court_case, "caseTitle") or case_number
        )
        source_url = _trusted_source_url(
            _first(row, "caseLink", "sourceUrl") or _first(court_case, "caseLink", "sourceUrl"),
            "uscourts.gov",
            PACER_CASE_LOCATOR_URL,
        )
        output.append(
            LegalCaseCandidate(
                source="pacer",
                feed="party_search",
                external_case_id=external_case_id,
                person_id=target.person_id,
                ticker=target.ticker,
                issuer_cik=target.issuer_cik,
                case_number=case_number,
                case_title=case_title,
                court=court or None,
                jurisdiction_type=str(
                    _first(row, "jurisdictionType") or _first(court_case, "jurisdictionType") or ""
                )
                or None,
                party_name=party_name,
                party_role=str(_first(row, "partyRole", "partyType") or "") or None,
                case_type=str(_first(row, "caseType") or _first(court_case, "caseType") or "")
                or None,
                nature_of_suit=str(
                    _first(row, "natureOfSuit") or _first(court_case, "natureOfSuit") or ""
                )
                or None,
                filed_at=str(_first(row, "dateFiled") or _first(court_case, "dateFiled") or "")
                or None,
                closed_at=str(
                    _first(row, "dateClosed", "effectiveDateClosed")
                    or _first(court_case, "dateClosed", "effectiveDateClosed")
                    or ""
                )
                or None,
                case_status=str(_first(row, "caseStatus") or _first(court_case, "caseStatus") or "")
                or None,
                source_url=source_url,
                name_match_confidence=min(match_confidence, target.link_confidence),
                match_method=f"reviewed_sec_link+{match_method}",
                payload=row,
            )
        )
    return tuple(output)


def parse_oalj_results(
    payload: dict[str, Any],
    target: LegalSearchTarget,
) -> tuple[LegalCaseCandidate, ...]:
    """Normalize metadata exported from the public OALJ search."""

    output: list[LegalCaseCandidate] = []
    for row in _rows(payload):
        party_name = _clean_name(_first(row, "party_name", "partyName", "employer", "name"))
        match = person_name_match(party_name, target.display_name)
        if match is None:
            continue
        confidence, method = match
        case_number = str(_first(row, "case_number", "caseNumber", "oalj_case_number") or "")
        if not case_number:
            continue
        source_url = _trusted_source_url(
            _first(row, "source_url", "url", "document_url"),
            "dol.gov",
            OALJ_SEARCH_URL,
        )
        output.append(
            LegalCaseCandidate(
                source="dol_oalj",
                feed="decision_search",
                external_case_id=case_number,
                person_id=target.person_id,
                ticker=target.ticker,
                issuer_cik=target.issuer_cik,
                case_number=case_number,
                case_title=_clean_name(_first(row, "title", "case_title") or case_number),
                court="DOL OALJ",
                jurisdiction_type="administrative",
                party_name=party_name,
                party_role=str(_first(row, "party_role", "partyRole") or "") or None,
                case_type=str(_first(row, "case_type", "caseType", "program_area") or "") or None,
                nature_of_suit=str(_first(row, "document_type", "documentType") or "") or None,
                filed_at=str(_first(row, "decision_date", "issue_date", "date") or "") or None,
                case_status=str(_first(row, "status", "disposition") or "") or None,
                source_url=source_url,
                name_match_confidence=min(confidence, target.link_confidence),
                match_method=f"reviewed_sec_link+{method}",
                payload=row,
            )
        )
    return tuple(output)


def record_legal_search(
    fetch: SourceFetch,
    candidates: Iterable[LegalCaseCandidate],
) -> dict[str, Any]:
    """Audit one source fetch and upsert its private review candidates."""

    candidate_list = list(candidates)
    if any((item.source, item.feed) != (fetch.source, fetch.feed) for item in candidate_list):
        raise ValueError("Legal candidates must match the fetch source and feed")
    run_id = record_source_batch(SourceBatch(fetch=fetch))
    collected_at = _iso(fetch.finished_at)
    try:
        with connection() as database:
            for item in candidate_list:
                approved_link = database.execute(
                    """
                    SELECT 1 FROM filing_people p
                    JOIN filing_person_issuer_links l ON l.person_id=p.id
                    WHERE p.id=? AND l.ticker=? AND l.issuer_cik=?
                      AND p.review_status='approved' AND l.review_status='approved'
                    LIMIT 1
                    """,
                    (item.person_id, item.ticker, item.issuer_cik),
                ).fetchone()
                if approved_link is None:
                    raise ValueError("Legal candidates require a reviewed person-to-issuer link")
                candidate_id = hashlib.sha256(
                    (
                        f"{item.source}:{item.feed}:{item.external_case_id}:"
                        f"{item.person_id}:{item.ticker}"
                    ).encode()
                ).hexdigest()
                database.execute(
                    """
                    INSERT INTO legal_case_candidates(
                        id,source,feed,external_case_id,person_id,ticker,issuer_cik,
                        case_number,case_title,court,jurisdiction_type,party_name,party_role,
                        case_type,nature_of_suit,filed_at,closed_at,case_status,source_url,
                        name_match_confidence,match_method,review_status,risk_label,
                        payload_json,first_run_id,last_run_id,first_collected_at,last_collected_at
                    ) VALUES(
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                        'pending','unknown',?,?,?,?,?
                    )
                    ON CONFLICT(source,feed,external_case_id,person_id,ticker) DO UPDATE SET
                        case_number=excluded.case_number,case_title=excluded.case_title,
                        court=excluded.court,jurisdiction_type=excluded.jurisdiction_type,
                        party_name=excluded.party_name,party_role=excluded.party_role,
                        case_type=excluded.case_type,nature_of_suit=excluded.nature_of_suit,
                        filed_at=excluded.filed_at,closed_at=excluded.closed_at,
                        case_status=excluded.case_status,source_url=excluded.source_url,
                        name_match_confidence=excluded.name_match_confidence,
                        match_method=excluded.match_method,payload_json=excluded.payload_json,
                        last_run_id=excluded.last_run_id,
                        last_collected_at=excluded.last_collected_at
                    """,
                    (
                        candidate_id,
                        item.source,
                        item.feed,
                        item.external_case_id,
                        item.person_id,
                        item.ticker,
                        item.issuer_cik,
                        item.case_number,
                        item.case_title,
                        item.court,
                        item.jurisdiction_type,
                        item.party_name,
                        item.party_role,
                        item.case_type,
                        item.nature_of_suit,
                        item.filed_at,
                        item.closed_at,
                        item.case_status,
                        item.source_url,
                        item.name_match_confidence,
                        item.match_method,
                        json.dumps(item.payload, sort_keys=True, separators=(",", ":")),
                        run_id,
                        run_id,
                        collected_at,
                        collected_at,
                    ),
                )
    except Exception as exc:
        with connection() as database:
            database.execute(
                "UPDATE ingestion_runs SET status='error',error=? WHERE id=?",
                (f"Legal projection failed: {exc}"[:1_000], run_id),
            )
        raise
    return {"run_id": run_id, "candidates": len(candidate_list), "status": fetch.status}


def review_legal_case_candidate(
    candidate_id: str,
    status: str,
    *,
    risk_label: str = "unknown",
    note: str = "",
) -> None:
    """Approve identity/relevance and separately label materiality."""

    if status not in REVIEW_STATUSES:
        raise ValueError("Unknown case review status")
    if risk_label not in RISK_LABELS:
        raise ValueError("Unknown legal risk label")
    if status != "approved" and risk_label != "unknown":
        raise ValueError("Only approved case matches can carry a legal risk label")
    with connection() as database:
        updated = database.execute(
            """
            UPDATE legal_case_candidates
            SET review_status=?,risk_label=?,review_note=? WHERE id=?
            """,
            (status, risk_label, note[:1_000] or None, candidate_id),
        )
        if updated.rowcount != 1:
            raise ValueError("Unknown legal case candidate")


def legal_risk_context(ticker: str) -> dict[str, Any]:
    """Return reviewed correlations only; no matches means unknown, not cleared."""

    symbol = ticker.strip().upper()
    with connection() as database:
        rows = database.execute(
            """
            SELECT c.*,p.display_name,p.sec_person_cik
            FROM legal_case_candidates c
            JOIN filing_people p ON p.id=c.person_id
            WHERE c.ticker=? AND c.review_status='approved'
              AND p.review_status='approved'
            ORDER BY CASE c.risk_label
                WHEN 'material' THEN 1 WHEN 'watch' THEN 2 ELSE 3 END,
                COALESCE(c.filed_at,c.last_collected_at) DESC
            LIMIT 50
            """,
            (symbol,),
        ).fetchall()
    cases = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json", "{}"))
        cases.append(item)
    return {
        "ticker": symbol,
        "coverage": "reviewed_matches" if cases else "no_reviewed_matches",
        "absence_is_not_clearance": not cases,
        "material_count": sum(item["risk_label"] == "material" for item in cases),
        "watch_count": sum(item["risk_label"] == "watch" for item in cases),
        "cases": cases,
    }
