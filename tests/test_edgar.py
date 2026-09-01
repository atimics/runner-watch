from __future__ import annotations

from pytest import MonkeyPatch

from runner_watch import edgar
from runner_watch.edgar import (
    LATEST_FILINGS_URL,
    EdgarClient,
    classify_filing,
    parse_beneficial_ownership_xml,
    parse_company_map,
    parse_latest_filings,
    parse_ownership_xml,
    primary_filing_document_names,
)


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_company_map_keeps_listed_exchanges() -> None:
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [10, "Listed Inc.", "LIST", "Nasdaq"],
            [11, "OTC Inc.", "OTCF", "OTC"],
            [12, "Dots Inc.", "DOT.A", "NYSE"],
        ],
    }
    companies = parse_company_map(payload)
    assert [company.ticker for company in companies] == ["LIST", "DOT-A"]


def test_atom_feed_deduplicates_accession_and_prefers_issuer() -> None:
    text = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>4 - Person (0000000011) (Reporting)</title>
        <link href="https://www.sec.gov/Archives/edgar/data/11/0001/0001-index.htm"/>
        <updated>2026-08-24T18:00:00-04:00</updated><category term="4"/>
        <id>urn:tag:sec.gov,2008:accession-number=0001-26-000001</id></entry>
      <entry><title>4 - Listed Inc. (0000000022) (Issuer)</title>
        <link href="https://www.sec.gov/Archives/edgar/data/22/0001/0001-index.htm"/>
        <updated>2026-08-24T18:00:00-04:00</updated><category term="4"/>
        <id>urn:tag:sec.gov,2008:accession-number=0001-26-000001</id></entry>
    </feed>"""
    filings = parse_latest_filings(text)
    assert len(filings) == 1
    assert filings[0].cik == 22
    assert filings[0].role == "Issuer"


def test_form4_parser_only_treats_code_p_as_purchase() -> None:
    text = """<ownershipDocument>
      <issuer><issuerCik>22</issuerCik><issuerTradingSymbol>PEN.N</issuerTradingSymbol></issuer>
      <reportingOwner><reportingOwnerId><rptOwnerCik>9876543</rptOwnerCik>
      <rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
      <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship></reportingOwner>
      <nonDerivativeTable>
        <nonDerivativeTransaction><transactionCoding><transactionCode>A</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>1000</value></transactionShares>
          <transactionPricePerShare><value>0</value></transactionPricePerShare>
          <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction><transactionCoding><transactionCode>P</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>5000</value></transactionShares>
          <transactionPricePerShare><value>2.50</value></transactionPricePerShare>
          <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
          <postTransactionAmounts><sharesOwnedFollowingTransaction><value>15000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
          <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
          <transactionAmounts><transactionShares><value>200</value></transactionShares>
          <transactionPricePerShare><value>3.25</value></transactionPricePerShare>
          <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>"""
    summary = parse_ownership_xml(text)
    assert summary.ticker == "PEN-N"
    assert summary.owner_cik == 9876543
    assert summary.owner_title == "Director"
    assert summary.purchase_shares == 5000
    assert summary.purchase_value == 12_500
    assert summary.average_purchase_price == 2.5
    assert summary.sale_shares == 200
    assert summary.sale_value == 650
    assert summary.average_sale_price == 3.25
    assert summary.post_transaction_shares == 15000
    assert summary.stake_change_pct == 50.0
    classification = classify_filing("4", summary)
    assert classification["sentiment"] == "neutral"
    assert classification["kind"] == "Insider purchase · check stake and financing context"


def test_offering_filings_are_risk_events() -> None:
    result = classify_filing("S-3")
    assert result == {"kind": "Offering or dilution filing", "sentiment": "risk", "score": 82}


def test_schedule_13_parser_keeps_concentration_neutral() -> None:
    text = """<edgarSubmission>
      <reportingPersonInfo>
        <nameOfReportingPerson>Patient Capital LP</nameOfReportingPerson>
        <aggregateAmountBeneficiallyOwnedByEachReportingPerson>6,500,000</aggregateAmountBeneficiallyOwnedByEachReportingPerson>
        <percentOfClassRepresentedByAmount>65.0%</percentOfClassRepresentedByAmount>
        <typeOfReportingPerson>IA</typeOfReportingPerson>
      </reportingPersonInfo>
    </edgarSubmission>"""
    summary = parse_beneficial_ownership_xml(text)
    assert summary is not None
    assert summary.owner_names == ("Patient Capital LP",)
    assert summary.ownership_pct == 65.0
    assert summary.beneficial_shares == 6_500_000
    assert classify_filing("SC 13D")["sentiment"] == "neutral"


def test_sec_download_emits_the_same_source_fetch_contract(monkeypatch: MonkeyPatch) -> None:
    body = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" />'
    monkeypatch.setattr(
        edgar.urllib.request, "urlopen", lambda request, timeout: FakeResponse(body)
    )
    fetches = []
    result = EdgarClient(
        max_requests_per_second=1_000_000,
        fetch_recorder=fetches.append,
    ).get_text(LATEST_FILINGS_URL)
    assert result.startswith("<?xml")
    assert len(fetches) == 1
    assert fetches[0].source == "sec"
    assert fetches[0].feed == "current_filings"
    assert fetches[0].status == "success"


def test_primary_filing_document_prefers_full_submission_text() -> None:
    index = {
        "directory": {
            "item": [
                {"name": "report.htm"},
                {"name": "company-8k.htm"},
                {"name": "000000000126000001.txt"},
            ]
        }
    }

    names = primary_filing_document_names(index, "0000000001-26-000001")

    assert names == ["000000000126000001.txt"]


def test_primary_filing_document_prefers_main_html_over_exhibit_text() -> None:
    index = {
        "directory": {
            "item": [
                {"name": "report.htm"},
                {"name": "exhibit.txt"},
                {"name": "company-8k.htm"},
            ]
        }
    }

    names = primary_filing_document_names(index, "0000000001-26-000001")

    assert names == ["company-8k.htm"]
