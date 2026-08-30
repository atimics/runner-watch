from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from runner_watch.ingestion import SourceFetch
from runner_web import db
from runner_web.db import connection, init_db
from runner_web.legal_risk import (
    legal_risk_context,
    legal_search_targets,
    pacer_party_search_criteria,
    parse_oalj_results,
    parse_pacer_party_results,
    record_legal_search,
    review_filing_person,
    review_filing_person_link,
    review_legal_case_candidate,
    sync_filing_people,
)
from runner_web.research_context import build_research_context
from runner_web.source_catalog import DEFAULT_SOURCE_POLICIES


def _insert_filing_people_source_rows() -> None:
    with connection() as database:
        database.executemany(
            """
            INSERT INTO sec_filings(
                accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                filing_url,actor,actor_cik,actor_title,beneficial_owner_names,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
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
                    "2026-08-20T20:00:00+00:00",
                    "https://www.sec.gov/Archives/edgar/data/22/1/1-index.htm",
                    "John Q. Smith Jr.",
                    9876543,
                    "Director",
                    "",
                    "2026-08-20T20:00:00+00:00",
                    "2026-08-20T20:00:00+00:00",
                ),
                (
                    "0000000022-26-000002",
                    22,
                    "PEN",
                    "Penny Labs",
                    "SC 13D",
                    "Beneficial ownership",
                    "neutral",
                    50.0,
                    "Schedule 13D",
                    "2026-08-19T20:00:00+00:00",
                    "https://www.sec.gov/Archives/edgar/data/22/2/2-index.htm",
                    None,
                    None,
                    None,
                    "Patient Capital LP,Jane Roe",
                    "2026-08-19T20:00:00+00:00",
                    "2026-08-19T20:00:00+00:00",
                ),
            ),
        )


def test_filing_people_require_review_before_legal_search(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "legal-people.db")
    init_db()
    _insert_filing_people_source_rows()

    result = sync_filing_people()

    assert result == {"filings": 2, "people_staged": 3, "links_staged": 3}
    assert legal_search_targets() == []
    with connection() as database:
        people = database.execute(
            "SELECT id,display_name,entity_type,sec_person_cik FROM filing_people "
            "ORDER BY display_name"
        ).fetchall()
    assert [(row["display_name"], row["entity_type"]) for row in people] == [
        ("Jane Roe", "person_candidate"),
        ("John Q. Smith Jr.", "person_candidate"),
        ("Patient Capital LP", "organization_candidate"),
    ]
    john = next(row for row in people if row["display_name"].startswith("John"))
    assert john["sec_person_cik"] == 9876543

    review_filing_person(john["id"], "approved", note="SEC owner CIK checked")
    assert legal_search_targets() == []
    review_filing_person_link(
        john["id"],
        "PEN",
        22,
        "approved",
        note="Filing issuer CIK and ticker checked",
    )
    targets = legal_search_targets()
    assert len(targets) == 1
    assert pacer_party_search_criteria(targets[0], filed_from="2020-01-01") == {
        "firstName": "john",
        "middleName": "q",
        "lastName": "smith",
        "courtCase": {"jurisdictionType": "cv", "dateFiledFrom": "2020-01-01"},
        "requestType": "Immediate",
        "requestSource": "Other",
    }


def test_pacer_and_oalj_results_stay_private_until_case_review(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "legal-cases.db")
    init_db()
    _insert_filing_people_source_rows()
    sync_filing_people()
    with connection() as database:
        person_id = database.execute(
            "SELECT id FROM filing_people WHERE sec_person_cik=9876543"
        ).fetchone()["id"]
    review_filing_person(person_id, "approved")
    review_filing_person_link(person_id, "PEN", 22, "approved")
    target = legal_search_targets()[0]

    pacer_payload = {
        "content": [
            {
                "caseId": 101,
                "courtId": "nysd",
                "caseNumberFull": "1:26-cv-00101",
                "caseTitle": "Example Investor v. John Q. Smith",
                "firstName": "John",
                "middleName": "Q",
                "lastName": "Smith",
                "partyRole": "Defendant",
                "jurisdictionType": "cv",
                "natureOfSuit": "Securities/Commodities/Exchange",
                "dateFiled": "2026-08-01",
                "caseLink": "javascript:alert(1)",
            },
            {
                "caseId": 102,
                "courtId": "nysd",
                "caseNumberFull": "1:26-cv-00102",
                "caseTitle": "Different person",
                "firstName": "John",
                "middleName": "R",
                "lastName": "Smith",
            },
        ]
    }
    candidates = parse_pacer_party_results(pacer_payload, target)
    assert len(candidates) == 1
    assert candidates[0].case_number == "1:26-cv-00101"
    assert candidates[0].name_match_confidence == 0.99
    assert candidates[0].source_url.startswith("https://pcl.uscourts.gov/")

    started_at = datetime(2026, 8, 30, 18, tzinfo=UTC)
    fetch = SourceFetch(
        source="pacer",
        feed="party_search",
        locator="https://pcl.uscourts.gov/pcl-public-api/rest/parties/find",
        status="success",
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        payload={"result_count": 2},
        content_type="application/json",
        metadata={"requested_count": 1, "received_count": 2, "billable_pages": 1},
    )
    record_legal_search(fetch, candidates)

    context = legal_risk_context("PEN")
    assert context["coverage"] == "no_reviewed_matches"
    assert context["absence_is_not_clearance"] is True
    with connection() as database:
        candidate_id = database.execute("SELECT id FROM legal_case_candidates").fetchone()["id"]
    review_legal_case_candidate(
        candidate_id,
        "approved",
        risk_label="watch",
        note="Identity and case role checked; no merits inference",
    )
    context = legal_risk_context("PEN")
    assert context["coverage"] == "reviewed_matches"
    assert context["watch_count"] == 1
    assert context["material_count"] == 0
    research = build_research_context(
        "PEN",
        {},
        token_budget=10_000,
        as_of="2026-08-31T00:00:00+00:00",
    )
    assert any(
        section["kind"] == "reviewed_legal_case_correlation"
        for section in research["context_sections"]
    )

    oalj = parse_oalj_results(
        {
            "results": [
                {
                    "case_number": "2026-SOX-00012",
                    "title": "Worker v. John Q Smith",
                    "party_name": "John Q Smith",
                    "party_role": "Respondent",
                    "case_type": "SOX",
                    "document_type": "Case Decision",
                    "decision_date": "2026-08-12",
                    "url": "https://www.dol.gov/agencies/oalj/PUBLIC/DECISIONS/CASE123.htm",
                }
            ]
        },
        target,
    )
    assert len(oalj) == 1
    assert oalj[0].jurisdiction_type == "administrative"


def test_rejected_match_cannot_be_labeled_as_risk(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "legal-review.db")
    init_db()
    with pytest.raises(ValueError, match="Only approved"):
        review_legal_case_candidate("missing", "rejected", risk_label="material")


def test_legal_sources_are_registered_as_internal_pilots() -> None:
    policies = {
        (policy.source, policy.feed): policy
        for policy in DEFAULT_SOURCE_POLICIES
        if policy.source in {"dol_oalj", "pacer", "courtlistener"}
    }
    assert set(policies) == {
        ("dol_oalj", "decision_search"),
        ("pacer", "party_search"),
        ("courtlistener", "recap_search"),
    }
    assert {policy.review_status for policy in policies.values()} == {"poc_only"}
    assert {policy.display_policy for policy in policies.values()} == {"internal_review_only"}
