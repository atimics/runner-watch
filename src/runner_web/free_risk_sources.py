from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from runner_watch.ingestion import SourceBatch, SourceFetch
from runner_web.ingestion import record_source_batch
from runner_web.legal_risk import (
    LegalCaseCandidate,
    LegalSearchTarget,
    legal_search_targets,
    normalize_person_name,
    person_name_match,
    record_legal_search,
)

USER_AGENT = "RunnerWatch/0.2 legal-risk ingestion https://stonks.rati.foundation"
OFAC_SDN_URL = (
    "https://sanctionslistservice.ofac.treas.gov/"
    "api/PublicationPreview/exports/SDN.XML"
)
OFAC_PUBLIC_URL = "https://ofac.treasury.gov/sanctions-list-service"
HHS_LEIE_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"
HHS_PUBLIC_URL = "https://www.oig.hhs.gov/exclusions/leie-database-supplement-downloads/"
SAM_EXCLUSIONS_URL = "https://api.sam.gov/entity-information/v4/exclusions"
SAM_PUBLIC_URL = "https://sam.gov/content/exclusions"
EVIDENCE_STATUSES = {
    "unverified",
    "complaint",
    "pending",
    "administrative",
    "final",
    "observational",
}
Download = Callable[[urllib.request.Request, float], tuple[bytes, str | None]]


@dataclass(frozen=True, slots=True)
class OfficialRiskProfile:
    source: str
    feed: str
    authority: str
    event_kind: str
    evidence_status: str
    allowed_hosts: tuple[str, ...]
    fallback_url: str


def _profile(
    source: str,
    feed: str,
    authority: str,
    event_kind: str,
    evidence_status: str,
    host: str,
    url: str,
) -> OfficialRiskProfile:
    return OfficialRiskProfile(
        source=source,
        feed=feed,
        authority=authority,
        event_kind=event_kind,
        evidence_status=evidence_status,
        allowed_hosts=(host,),
        fallback_url=url,
    )


OFFICIAL_RISK_PROFILES = {
    (profile.source, profile.feed): profile
    for profile in (
        _profile(
            "govinfo",
            "uscourts_opinions",
            "U.S. Government Publishing Office",
            "court_opinion",
            "final",
            "govinfo.gov",
            "https://www.govinfo.gov/help/uscourts",
        ),
        _profile(
            "sec",
            "enforcement_litigation",
            "U.S. Securities and Exchange Commission",
            "securities_enforcement",
            "complaint",
            "sec.gov",
            "https://www.sec.gov/enforcement-litigation/litigation-releases",
        ),
        _profile(
            "doj",
            "corporate_enforcement",
            "U.S. Department of Justice",
            "criminal_enforcement",
            "unverified",
            "justice.gov",
            "https://www.justice.gov/criminal/corporate-enforcement",
        ),
        _profile(
            "ftc",
            "cases",
            "Federal Trade Commission",
            "consumer_or_competition_case",
            "unverified",
            "ftc.gov",
            "https://www.ftc.gov/legal-library/browse/cases-proceedings",
        ),
        _profile(
            "pcaob",
            "enforcement_actions",
            "Public Company Accounting Oversight Board",
            "audit_disciplinary_action",
            "final",
            "pcaobus.org",
            "https://pcaobus.org/oversight/enforcement/enforcement-actions",
        ),
        _profile(
            "occ",
            "enforcement_actions",
            "Office of the Comptroller of the Currency",
            "bank_enforcement",
            "unverified",
            "occ.gov",
            "https://www.occ.gov/topics/laws-and-regulations/enforcement-actions/"
            "index-enforcement-actions.html",
        ),
        _profile(
            "fdic",
            "enforcement_orders",
            "Federal Deposit Insurance Corporation",
            "bank_enforcement",
            "unverified",
            "fdic.gov",
            "https://orders.fdic.gov/s/",
        ),
        _profile(
            "finra",
            "disciplinary_actions",
            "Financial Industry Regulatory Authority",
            "broker_disciplinary_action",
            "unverified",
            "finra.org",
            "https://www.finra.org/rules-guidance/oversight-enforcement/"
            "finra-disciplinary-actions-online",
        ),
        OfficialRiskProfile(
            source="ofac",
            feed="sanctions_sdn",
            authority="U.S. Treasury Office of Foreign Assets Control",
            event_kind="sanction",
            evidence_status="final",
            allowed_hosts=("ofac.treasury.gov", "sanctionslistservice.ofac.treas.gov"),
            fallback_url=OFAC_PUBLIC_URL,
        ),
        _profile(
            "sam",
            "exclusions",
            "U.S. General Services Administration",
            "federal_exclusion",
            "unverified",
            "sam.gov",
            SAM_PUBLIC_URL,
        ),
        _profile(
            "hhs_oig",
            "leie",
            "HHS Office of Inspector General",
            "healthcare_exclusion",
            "final",
            "hhs.gov",
            HHS_PUBLIC_URL,
        ),
        _profile(
            "epa",
            "echo_enforcement",
            "U.S. Environmental Protection Agency",
            "environmental_enforcement",
            "unverified",
            "epa.gov",
            "https://echo.epa.gov/tools/data-downloads",
        ),
        _profile(
            "osha",
            "establishment_inspections",
            "Occupational Safety and Health Administration",
            "workplace_inspection",
            "observational",
            "osha.gov",
            "https://www.osha.gov/ords/imis/establishment.html",
        ),
        _profile(
            "nlrb",
            "cases",
            "National Labor Relations Board",
            "labor_case",
            "unverified",
            "nlrb.gov",
            "https://www.nlrb.gov/advanced-search",
        ),
        _profile(
            "fda",
            "enforcement_recalls",
            "U.S. Food and Drug Administration",
            "recall_or_enforcement",
            "observational",
            "fda.gov",
            "https://www.fda.gov/about-fda/open-government-fda-data-sets/"
            "recalls-data-sets",
        ),
        _profile(
            "usitc",
            "edis_investigations",
            "U.S. International Trade Commission",
            "trade_or_ip_investigation",
            "unverified",
            "usitc.gov",
            "https://www.usitc.gov/intellectual_property.htm",
        ),
        _profile(
            "cfpb",
            "complaints",
            "Consumer Financial Protection Bureau",
            "consumer_complaint",
            "complaint",
            "consumerfinance.gov",
            "https://www.consumerfinance.gov/data-research/consumer-complaints/",
        ),
        _profile(
            "cms",
            "open_payments",
            "Centers for Medicare & Medicaid Services",
            "industry_payment",
            "observational",
            "cms.gov",
            "https://openpaymentsdata.cms.gov/",
        ),
        _profile(
            "fec",
            "contributions",
            "Federal Election Commission",
            "political_contribution",
            "observational",
            "fec.gov",
            "https://api.open.fec.gov/developers/",
        ),
        _profile(
            "usaspending",
            "awards",
            "U.S. Department of the Treasury",
            "federal_award",
            "observational",
            "usaspending.gov",
            "https://www.usaspending.gov/",
        ),
    )
}


def free_legal_sources_enabled() -> bool:
    return os.getenv("FREE_LEGAL_SOURCES_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _enabled(name: str) -> bool:
    return free_legal_sources_enabled() and os.getenv(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _download(request: urllib.request.Request, timeout: float) -> tuple[bytes, str | None]:
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read(), response.headers.get_content_type()


def _request(url: str, *, accept: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": accept},
    )


def _clean(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _trusted_url(value: Any, profile: OfficialRiskProfile) -> str:
    candidate = str(value or "").strip()
    parsed = urlparse(candidate)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and any(
        hostname == allowed or hostname.endswith(f".{allowed}")
        for allowed in profile.allowed_hosts
    ):
        return candidate
    return profile.fallback_url


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("records", "results", "items", "data", "content"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _rows(value)
            if nested:
                return nested
    return []


def _subject_names(row: dict[str, Any]) -> tuple[str, ...]:
    raw_names = row.get("subject_names")
    names = raw_names if isinstance(raw_names, list) else []
    names = [
        *names,
        row.get("subject_name"),
        row.get("party_name"),
        row.get("respondent"),
        row.get("defendant"),
        row.get("individual_name"),
        row.get("name"),
    ]
    output: list[str] = []
    seen: set[str] = set()
    for name in names:
        cleaned = _clean(name, 240)
        normalized = normalize_person_name(cleaned)
        if cleaned and normalized and normalized not in seen:
            output.append(cleaned)
            seen.add(normalized)
    return tuple(output)


def _stable_external_id(profile: OfficialRiskProfile, row: dict[str, Any]) -> str:
    for key in (
        "external_id",
        "record_id",
        "id",
        "uid",
        "docket_number",
        "case_number",
        "matter_number",
    ):
        value = _clean(row.get(key), 240)
        if value:
            return value
    identity = {
        "source": profile.source,
        "feed": profile.feed,
        "names": _subject_names(row),
        "title": _clean(row.get("title"), 500),
        "date": _clean(row.get("date"), 50),
    }
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def parse_official_risk_results(
    payload: dict[str, Any],
    targets: Iterable[LegalSearchTarget],
    *,
    source: str,
    feed: str,
) -> tuple[LegalCaseCandidate, ...]:
    """Stage exact person matches from one normalized official-source payload."""

    try:
        profile = OFFICIAL_RISK_PROFILES[(source, feed)]
    except KeyError as exc:
        raise ValueError(f"Unknown official risk source: {source}/{feed}") from exc
    target_index: dict[tuple[str, str], list[LegalSearchTarget]] = {}
    for target in targets:
        words = target.normalized_name.split()
        if len(words) >= 2:
            target_index.setdefault((words[0], words[-1]), []).append(target)

    output: list[LegalCaseCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for row in _rows(payload):
        external_id = _stable_external_id(profile, row)
        for subject_name in _subject_names(row):
            words = normalize_person_name(subject_name).split()
            if len(words) < 2:
                continue
            for target in target_index.get((words[0], words[-1]), []):
                match = person_name_match(subject_name, target.display_name)
                if match is None:
                    continue
                confidence, method = match
                identity = (external_id, target.person_id, target.ticker)
                if identity in seen:
                    continue
                seen.add(identity)
                evidence_status = _clean(
                    row.get("evidence_status") or profile.evidence_status,
                    40,
                ).lower()
                if evidence_status not in EVIDENCE_STATUSES:
                    evidence_status = profile.evidence_status
                case_number = _clean(
                    row.get("docket_number")
                    or row.get("case_number")
                    or row.get("matter_number")
                    or external_id,
                    240,
                )
                title = _clean(
                    row.get("title")
                    or row.get("case_title")
                    or f"{profile.authority} {profile.event_kind.replace('_', ' ')}",
                    500,
                )
                normalized_payload = dict(row)
                normalized_payload["_risk_event_kind"] = profile.event_kind
                normalized_payload["_evidence_status"] = evidence_status
                normalized_payload["_authority"] = profile.authority
                output.append(
                    LegalCaseCandidate(
                        source=source,
                        feed=feed,
                        external_case_id=external_id,
                        person_id=target.person_id,
                        ticker=target.ticker,
                        issuer_cik=target.issuer_cik,
                        case_number=case_number,
                        case_title=title,
                        court=profile.authority,
                        jurisdiction_type="official_record",
                        party_name=subject_name,
                        party_role=_clean(row.get("subject_role") or row.get("party_role"), 120)
                        or None,
                        case_type=profile.event_kind,
                        nature_of_suit=_clean(row.get("category"), 240) or None,
                        filed_at=_clean(row.get("date") or row.get("filed_at"), 50) or None,
                        closed_at=_clean(row.get("end_date") or row.get("closed_at"), 50)
                        or None,
                        case_status=_clean(row.get("status") or evidence_status, 120) or None,
                        source_url=_trusted_url(row.get("source_url"), profile),
                        name_match_confidence=min(confidence, target.link_confidence),
                        match_method=(
                            f"reviewed_sec_link+{method}+official_{source}_record"
                        ),
                        payload=normalized_payload,
                    )
                )
    return tuple(output)


def ingest_official_risk_payload(
    payload: dict[str, Any],
    *,
    source: str,
    feed: str,
    locator: str,
    targets: list[LegalSearchTarget] | None = None,
    started_at: datetime | None = None,
) -> dict[str, Any]:
    """Audit one normalized official archive import and stage private candidates."""

    try:
        profile = OFFICIAL_RISK_PROFILES[(source, feed)]
    except KeyError as exc:
        raise ValueError(f"Unknown official risk source: {source}/{feed}") from exc
    selected_targets = targets if targets is not None else legal_search_targets(500)
    candidates = parse_official_risk_results(
        payload,
        selected_targets,
        source=source,
        feed=feed,
    )
    fetch = SourceFetch.success(
        source=source,
        feed=feed,
        locator=_trusted_url(locator, profile),
        started_at=started_at or datetime.now(UTC),
        payload=payload,
        content_type="application/json",
        metadata={
            "requested_count": len(selected_targets),
            "received_count": len(candidates),
            "source_records": len(_rows(payload)),
            "normalized_import": True,
        },
    )
    result = record_legal_search(fetch, candidates)
    result["source_records"] = len(_rows(payload))
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _direct_text(node: ET.Element, name: str) -> str:
    for child in node:
        if _local_name(child.tag) == name:
            return _clean(child.text, 500)
    return ""


def _person_name(node: ET.Element) -> str:
    return _clean(
        " ".join(
            value
            for value in (
                _direct_text(node, "firstName"),
                _direct_text(node, "middleName"),
                _direct_text(node, "lastName"),
            )
            if value
        ),
        240,
    )


def parse_ofac_sdn_xml(body: bytes) -> dict[str, list[dict[str, Any]]]:
    """Normalize OFAC's official SDN XML without treating a name as an identity."""

    root = ET.fromstring(body)
    records: list[dict[str, Any]] = []
    for entry in root.iter():
        if _local_name(entry.tag) != "sdnEntry":
            continue
        uid = _direct_text(entry, "uid")
        primary_name = _person_name(entry)
        aliases: list[str] = []
        programs: list[str] = []
        for descendant in entry.iter():
            local = _local_name(descendant.tag)
            if local == "aka":
                alias = _person_name(descendant)
                if alias:
                    aliases.append(alias)
            elif local == "program" and descendant.text:
                programs.append(_clean(descendant.text, 120))
        names = [name for name in (primary_name, *aliases) if name]
        if not uid or not names:
            continue
        records.append(
            {
                "external_id": f"ofac-sdn:{uid}",
                "subject_names": names,
                "subject_name": primary_name or names[0],
                "title": f"OFAC SDN designation: {primary_name or names[0]}",
                "category": ", ".join(dict.fromkeys(programs)),
                "status": "active",
                "evidence_status": "final",
                "source_url": OFAC_PUBLIC_URL,
                "record_id": uid,
            }
        )
    return {"records": records}


def _date(value: Any) -> str:
    text = _clean(value, 30)
    if not text or not text.strip("0"):
        return ""
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def parse_hhs_leie_csv(body: bytes) -> dict[str, list[dict[str, Any]]]:
    """Normalize the current HHS exclusion CSV and preserve its public identifiers."""

    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig", errors="replace")))
    records: list[dict[str, Any]] = []
    for raw in reader:
        row = {str(key or "").upper(): value for key, value in raw.items()}
        person_name = _clean(
            " ".join(
                value
                for value in (
                    row.get("FIRSTNAME"),
                    row.get("MIDNAME"),
                    row.get("LASTNAME"),
                )
                if _clean(value)
            ),
            240,
        )
        subject_name = person_name or _clean(row.get("BUSNAME"), 240)
        if not subject_name:
            continue
        exclusion_type = _clean(row.get("EXCLTYPE"), 120)
        exclusion_date = _date(row.get("EXCLDATE"))
        public_id = next(
            (
                value
                for value in (
                    _clean(row.get("NPI"), 100),
                    _clean(row.get("UPIN"), 100),
                )
                if value and value.strip("0")
            ),
            "",
        )
        if not public_id:
            public_id = hashlib.sha256(
                f"{subject_name}:{exclusion_type}:{exclusion_date}".encode()
            ).hexdigest()[:24]
        records.append(
            {
                "external_id": f"hhs-leie:{public_id}:{exclusion_date}",
                "subject_name": subject_name,
                "title": f"HHS program exclusion: {subject_name}",
                "category": exclusion_type,
                "date": exclusion_date,
                "end_date": _date(row.get("REINDATE")),
                "status": "active",
                "evidence_status": "final",
                "source_url": HHS_PUBLIC_URL,
                "npi": _clean(row.get("NPI"), 40),
                "specialty": _clean(row.get("SPECIALTY"), 160),
                "state": _clean(row.get("STATE"), 20),
            }
        )
    return {"records": records}


def _mappings(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _mappings(child)


def _nested_value(row: dict[str, Any], *keys: str) -> Any:
    for mapping in _mappings(row):
        for key in keys:
            value = mapping.get(key)
            if value not in (None, "", []):
                return value
    return None


def parse_sam_exclusions(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Normalize the public SAM exclusions response for the review queue."""

    raw_records = payload.get("excludedEntity")
    if isinstance(raw_records, dict):
        raw_records = [raw_records]
    if not isinstance(raw_records, list):
        raw_records = _rows(payload)
    records: list[dict[str, Any]] = []
    for row in raw_records:
        if not isinstance(row, dict):
            continue
        exclusion_name = _clean(_nested_value(row, "exclusionName", "name", "entityName"), 240)
        first = _clean(_nested_value(row, "firstName"), 100)
        middle = _clean(_nested_value(row, "middleName"), 100)
        last = _clean(_nested_value(row, "lastName"), 100)
        person_name = _clean(" ".join(value for value in (first, middle, last) if value), 240)
        names = list(dict.fromkeys(name for name in (person_name, exclusion_name) if name))
        if not names:
            continue
        exclusion_type = _clean(_nested_value(row, "exclusionType"), 160)
        evidence_status = "pending" if "pending" in exclusion_type.lower() else "final"
        public_id = _clean(_nested_value(row, "ueiSAM", "cageCode", "npi"), 100)
        if not public_id:
            public_id = hashlib.sha256(
                f"{names[0]}:{exclusion_type}:{_nested_value(row, 'activeDate')}".encode()
            ).hexdigest()[:24]
        records.append(
            {
                "external_id": f"sam-exclusion:{public_id}",
                "subject_names": names,
                "subject_name": names[0],
                "title": f"SAM.gov exclusion: {names[0]}",
                "category": exclusion_type,
                "date": _date(_nested_value(row, "activeDate", "createdDate")),
                "end_date": _date(_nested_value(row, "terminationDate")),
                "status": exclusion_type or "listed",
                "evidence_status": evidence_status,
                "source_url": SAM_PUBLIC_URL,
                "program": _clean(_nested_value(row, "exclusionProgram"), 120),
                "excluding_agency": _clean(
                    _nested_value(row, "excludingAgencyName", "excludingAgencyCode"),
                    160,
                ),
                "uei": _clean(_nested_value(row, "ueiSAM"), 40),
            }
        )
    return {"records": records}


def _record_failure(
    source: str,
    feed: str,
    locator: str,
    started_at: datetime,
    error: Exception,
    *,
    requested_count: int,
) -> str:
    return record_source_batch(
        SourceBatch(
            fetch=SourceFetch.failure(
                source=source,
                feed=feed,
                locator=locator,
                started_at=started_at,
                error=error,
                metadata={"requested_count": requested_count},
            )
        )
    )


def _refresh_bulk_source(
    *,
    source: str,
    feed: str,
    locator: str,
    accept: str,
    parser: Callable[[bytes], dict[str, list[dict[str, Any]]]],
    timeout: float,
    download: Download,
    targets: list[LegalSearchTarget] | None = None,
    archive_raw: bool = False,
) -> dict[str, Any]:
    selected_targets = targets if targets is not None else legal_search_targets(500)
    if not selected_targets:
        return {"status": "skipped", "reason": "no_reviewed_targets", "candidates": 0}
    started_at = datetime.now(UTC)
    try:
        body, content_type = download(_request(locator, accept=accept), timeout)
        payload = parser(body)
        candidates = parse_official_risk_results(
            payload,
            selected_targets,
            source=source,
            feed=feed,
        )
    except Exception as exc:
        run_id = _record_failure(
            source,
            feed,
            locator,
            started_at,
            exc,
            requested_count=len(selected_targets),
        )
        raise RuntimeError(f"{source} risk refresh failed in run {run_id}: {exc}") from exc
    fetch = SourceFetch.success(
        source=source,
        feed=feed,
        locator=locator,
        started_at=started_at,
        payload=(
            body
            if archive_raw
            else {
                "body_sha256": hashlib.sha256(body).hexdigest(),
                "source_records": len(payload["records"]),
            }
        ),
        content_type=(content_type if archive_raw else "application/json"),
        metadata={
            "requested_count": len(selected_targets),
            "received_count": len(candidates),
            "source_records": len(payload["records"]),
        },
    )
    result = record_legal_search(fetch, candidates)
    result["source_records"] = len(payload["records"])
    return result


def refresh_ofac_sdn(
    *,
    timeout: float = 45,
    download: Download = _download,
    targets: list[LegalSearchTarget] | None = None,
) -> dict[str, Any]:
    return _refresh_bulk_source(
        source="ofac",
        feed="sanctions_sdn",
        locator=OFAC_SDN_URL,
        accept="application/xml,text/xml",
        parser=parse_ofac_sdn_xml,
        timeout=timeout,
        download=download,
        targets=targets,
    )


def refresh_hhs_leie(
    *,
    timeout: float = 60,
    download: Download = _download,
    targets: list[LegalSearchTarget] | None = None,
) -> dict[str, Any]:
    return _refresh_bulk_source(
        source="hhs_oig",
        feed="leie",
        locator=HHS_LEIE_URL,
        accept="text/csv",
        parser=parse_hhs_leie_csv,
        timeout=timeout,
        download=download,
        targets=targets,
    )


def refresh_sam_exclusions(
    *,
    api_key: str | None = None,
    max_targets: int = 5,
    timeout: float = 20,
    download: Download = _download,
) -> dict[str, Any]:
    """Use a small daily request budget because basic SAM keys allow few requests."""

    key = (api_key or os.getenv("SAM_API_KEY", "")).strip()
    if not key:
        raise RuntimeError("SAM_API_KEY is required for SAM exclusion searches")
    targets = legal_search_targets(max(1, min(max_targets, 10)))
    runs: list[str] = []
    candidate_count = 0
    errors = 0
    for target in targets:
        public_query = urllib.parse.urlencode({"exclusionName": target.display_name, "size": 10})
        locator = f"{SAM_EXCLUSIONS_URL}?{public_query}"
        request_query = urllib.parse.urlencode(
            {"api_key": key, "exclusionName": target.display_name, "size": 10}
        )
        started_at = datetime.now(UTC)
        try:
            body, _content_type = download(
                _request(f"{SAM_EXCLUSIONS_URL}?{request_query}", accept="application/json"),
                timeout,
            )
            raw = json.loads(body.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("SAM exclusions response is not a JSON object")
            payload = parse_sam_exclusions(raw)
            candidates = parse_official_risk_results(
                payload,
                [target],
                source="sam",
                feed="exclusions",
            )
            fetch = SourceFetch.success(
                source="sam",
                feed="exclusions",
                locator=locator,
                started_at=started_at,
                payload={
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                    "source_records": len(payload["records"]),
                },
                content_type="application/json",
                metadata={
                    "requested_count": 1,
                    "received_count": len(candidates),
                    "source_records": len(payload["records"]),
                },
            )
            result = record_legal_search(fetch, candidates)
            runs.append(str(result["run_id"]))
            candidate_count += len(candidates)
        except Exception as exc:
            safe_error = RuntimeError(str(exc).replace(key, "[redacted]"))
            runs.append(
                _record_failure(
                    "sam",
                    "exclusions",
                    locator,
                    started_at,
                    safe_error,
                    requested_count=1,
                )
            )
            errors += 1
    return {
        "status": "partial" if errors else "success",
        "runs": runs,
        "targets": len(targets),
        "candidates": candidate_count,
        "errors": errors,
    }


def refresh_free_legal_sources() -> dict[str, Any]:
    """Run only explicitly enabled official collectors; all matches remain private."""

    if not free_legal_sources_enabled():
        return {"status": "disabled", "sources": {}}
    results: dict[str, Any] = {}
    if _enabled("OFAC_LEGAL_RISK_ENABLED"):
        results["ofac"] = refresh_ofac_sdn()
    if _enabled("HHS_LEIE_LEGAL_RISK_ENABLED"):
        results["hhs_oig"] = refresh_hhs_leie()
    if _enabled("SAM_EXCLUSIONS_LEGAL_RISK_ENABLED") and os.getenv("SAM_API_KEY", "").strip():
        results["sam"] = refresh_sam_exclusions()
    return {"status": "success", "sources": results}
