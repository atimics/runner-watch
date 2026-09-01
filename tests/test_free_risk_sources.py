import json
from pathlib import Path
from typing import Any

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.free_risk_sources import (
    HHS_PUBLIC_URL,
    OFAC_PUBLIC_URL,
    OFFICIAL_RISK_PROFILES,
    SAM_PUBLIC_URL,
    ingest_official_risk_payload,
    parse_hhs_leie_csv,
    parse_ofac_sdn_xml,
    parse_official_risk_results,
    parse_sam_exclusions,
    refresh_ofac_sdn,
    refresh_sam_exclusions,
)
from runner_web.legal_risk import (
    LegalSearchTarget,
    legal_search_targets,
    review_filing_person,
    review_filing_person_link,
    sync_filing_people,
)
from runner_web.source_catalog import DEFAULT_SOURCE_POLICIES


def _target() -> LegalSearchTarget:
    return LegalSearchTarget(
        person_id="person-1",
        display_name="John Q. Smith Jr.",
        normalized_name="john q smith",
        sec_person_cik=9876543,
        ticker="PEN",
        issuer_cik=22,
        filing_role="Director",
        link_confidence=0.99,
    )


def _approved_database_target(tmp_path: Path, monkeypatch: MonkeyPatch) -> LegalSearchTarget:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "free-risk.db")
    init_db()
    timestamp = "2026-08-20T20:00:00+00:00"
    with connection() as database:
        database.execute(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,actor,actor_cik,actor_title,beneficial_owner_names,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "0000000022-26-000001",
                22,
                "PEN",
                "Penny Labs",
                "4",
                "Insider transaction",
                "neutral",
                50.0,
                "Form 4",
                timestamp,
                "https://www.sec.gov/Archives/edgar/data/22/1/1-index.htm",
                "John Q. Smith Jr.",
                9876543,
                "Director",
                "",
                timestamp,
                timestamp,
            ),
        )
    sync_filing_people()
    with connection() as database:
        person_id = database.execute(
            "SELECT id FROM filing_people WHERE sec_person_cik=9876543"
        ).fetchone()["id"]
    review_filing_person(person_id, "approved")
    review_filing_person_link(person_id, "PEN", 22, "approved")
    return legal_search_targets()[0]


def test_free_official_sources_are_private_opt_in_pilots() -> None:
    expected = {
        "ofac",
        "sam",
        "hhs_oig",
        "govinfo",
        "sec",
        "doj",
        "ftc",
        "pcaob",
        "occ",
        "fdic",
        "finra",
        "epa",
        "osha",
        "nlrb",
        "fda",
        "usitc",
        "cfpb",
        "gleif",
        "usaspending",
        "cms",
        "fec",
    }
    policies = [
        policy
        for policy in DEFAULT_SOURCE_POLICIES
        if policy.source in expected
        and policy.feed
        not in {
            "company_map",
            "current_filings",
            "company_facts",
            "filing_index",
            "filing_document",
            "document",
        }
    ]
    assert {policy.source for policy in policies} == expected
    assert {policy.review_status for policy in policies} == {"poc_only"}
    assert {policy.display_policy for policy in policies} == {"internal_review_only"}
    assert not any(policy.enabled for policy in policies)


def test_normalized_official_records_only_stage_strong_name_matches() -> None:
    candidates = parse_official_risk_results(
        {
            "records": [
                {
                    "external_id": "LR-100",
                    "subject_name": "John Q Smith",
                    "title": "SEC v. John Q Smith",
                    "status": "pending",
                    "source_url": "javascript:alert(1)",
                },
                {
                    "external_id": "LR-101",
                    "subject_name": "John R Smith",
                    "title": "Different person",
                },
            ]
        },
        [_target()],
        source="sec",
        feed="enforcement_litigation",
    )
    assert len(candidates) == 1
    assert candidates[0].external_case_id == "LR-100"
    assert candidates[0].source_url.startswith("https://www.sec.gov/")
    assert candidates[0].payload["_evidence_status"] == "complaint"
    assert candidates[0].name_match_confidence == 0.99


def test_ofac_hhs_and_sam_normalizers_preserve_official_record_meaning() -> None:
    ofac = parse_ofac_sdn_xml(
        b"""<?xml version="1.0"?>
        <sdnList><sdnEntry><uid>77</uid><firstName>John</firstName>
        <middleName>Q</middleName><lastName>Smith</lastName><sdnType>Individual</sdnType>
        <programList><program>TEST</program></programList><akaList><aka>
        <firstName>Johnny</firstName><lastName>Smith</lastName></aka></akaList>
        </sdnEntry></sdnList>"""
    )
    assert ofac["records"][0]["external_id"] == "ofac-sdn:77"
    assert ofac["records"][0]["subject_names"] == ["John Q Smith", "Johnny Smith"]
    assert ofac["records"][0]["source_url"] == OFAC_PUBLIC_URL

    hhs = parse_hhs_leie_csv(
        b"LASTNAME,FIRSTNAME,MIDNAME,BUSNAME,GENERAL,SPECIALTY,UPIN,NPI,"
        b"EXCLTYPE,EXCLDATE,REINDATE,STATE\n"
        b"SMITH,JOHN,Q,,PHYSICIAN,CARDIOLOGY,U1,1234567890,1128a1,20260131,0,WA\n"
    )
    assert hhs["records"][0]["subject_name"] == "JOHN Q SMITH"
    assert hhs["records"][0]["date"] == "2026-01-31"
    assert hhs["records"][0]["source_url"] == HHS_PUBLIC_URL

    no_public_id = parse_hhs_leie_csv(
        b"LASTNAME,FIRSTNAME,MIDNAME,NPI,UPIN,EXCLTYPE,EXCLDATE\n"
        b"SMITH,JOHN,Q,0000000000,0000000000,1128a1,20260131\n"
    )
    assert "0000000000" not in no_public_id["records"][0]["external_id"]

    sam = parse_sam_exclusions(
        {
            "excludedEntity": [
                {
                    "exclusionDetails": {
                        "exclusionType": "Ineligible (Proceedings Completed)",
                        "excludingAgencyName": "DEPT OF TEST",
                    },
                    "exclusionIdentification": {
                        "ueiSAM": "UEI123",
                        "firstName": "John",
                        "middleName": "Q",
                        "lastName": "Smith",
                    },
                }
            ]
        }
    )
    assert sam["records"][0]["external_id"] == "sam-exclusion:UEI123"
    assert sam["records"][0]["evidence_status"] == "final"
    assert sam["records"][0]["source_url"] == SAM_PUBLIC_URL


def test_ofac_refresh_keeps_only_a_source_hash_and_pending_match(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = _approved_database_target(tmp_path, monkeypatch)
    body = b"""<?xml version="1.0"?>
    <sdnList><sdnEntry><uid>77</uid><firstName>John</firstName>
    <middleName>Q</middleName><lastName>Smith</lastName><sdnType>Individual</sdnType>
    <programList><program>TEST</program></programList></sdnEntry></sdnList>"""

    result = refresh_ofac_sdn(
        targets=[target],
        download=lambda _request, _timeout: (body, "text/xml"),
    )

    assert result["candidates"] == 1
    with connection() as database:
        candidate = database.execute(
            "SELECT source,feed,review_status,risk_label FROM legal_case_candidates"
        ).fetchone()
        documents = database.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
    assert tuple(candidate) == ("ofac", "sanctions_sdn", "pending", "unknown")
    assert documents == 0


def test_normalized_archive_import_is_audited_and_private(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    target = _approved_database_target(tmp_path, monkeypatch)
    result = ingest_official_risk_payload(
        {
            "records": [
                {
                    "external_id": "PCAOB-44",
                    "subject_name": "John Q Smith",
                    "title": "Settled disciplinary order",
                    "date": "2026-08-01",
                    "source_url": "https://pcaobus.org/oversight/enforcement/order-44",
                }
            ]
        },
        source="pcaob",
        feed="enforcement_actions",
        locator="https://pcaobus.org/oversight/enforcement/order-44",
        targets=[target],
    )
    assert result["candidates"] == 1
    with connection() as database:
        candidate = database.execute(
            "SELECT review_status,risk_label FROM legal_case_candidates "
            "WHERE source='pcaob'"
        ).fetchone()
        run = database.execute(
            "SELECT status,metadata_json FROM ingestion_runs WHERE source='pcaob'"
        ).fetchone()
    assert tuple(candidate) == ("pending", "unknown")
    assert run["status"] == "success"
    assert json.loads(run["metadata_json"])["normalized_import"] is True


def test_sam_api_key_is_never_written_to_ingestion_locator(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _approved_database_target(tmp_path, monkeypatch)
    requested_urls: list[str] = []

    def fake_download(request: Any, _timeout: float) -> tuple[bytes, str]:
        requested_urls.append(request.full_url)
        payload = {
            "excludedEntity": [
                {
                    "exclusionDetails": {
                        "exclusionType": "Ineligible (Proceedings Completed)"
                    },
                    "exclusionIdentification": {
                        "ueiSAM": "UEI123",
                        "firstName": "John",
                        "middleName": "Q",
                        "lastName": "Smith",
                    },
                }
            ]
        }
        return json.dumps(payload).encode(), "application/json"

    result = refresh_sam_exclusions(
        api_key="top-secret-key",
        max_targets=1,
        download=fake_download,
    )

    assert result["candidates"] == 1
    assert "top-secret-key" in requested_urls[0]
    with connection() as database:
        locator = database.execute(
            "SELECT locator FROM ingestion_runs WHERE source='sam'"
        ).fetchone()["locator"]
    assert "top-secret-key" not in locator
    assert "exclusionName=John+Q.+Smith+Jr." in locator


def test_sam_api_key_is_redacted_from_recorded_errors(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    _approved_database_target(tmp_path, monkeypatch)

    def failed_download(_request: Any, _timeout: float) -> tuple[bytes, str]:
        raise RuntimeError("request with top-secret-key failed")

    result = refresh_sam_exclusions(
        api_key="top-secret-key",
        max_targets=1,
        download=failed_download,
    )

    assert result["status"] == "partial"
    with connection() as database:
        error = database.execute(
            "SELECT error FROM ingestion_runs WHERE source='sam'"
        ).fetchone()["error"]
    assert "top-secret-key" not in error
    assert "[redacted]" in error


def test_every_ingestible_profile_has_a_registered_policy() -> None:
    policies = {(policy.source, policy.feed) for policy in DEFAULT_SOURCE_POLICIES}
    assert set(OFFICIAL_RISK_PROFILES) <= policies
