from runner_watch.edgar import (
    classify_filing,
    parse_company_map,
    parse_latest_filings,
    parse_ownership_xml,
)


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
      <reportingOwner><reportingOwnerId><rptOwnerName>Jane Doe</rptOwnerName></reportingOwnerId>
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
    assert summary.owner_title == "Director"
    assert summary.purchase_shares == 5000
    assert summary.purchase_value == 12_500
    assert summary.average_purchase_price == 2.5
    assert summary.sale_shares == 200
    assert summary.sale_value == 650
    assert summary.average_sale_price == 3.25
    assert classify_filing("4", summary)["kind"] == "Insider open-market buy"


def test_offering_filings_are_risk_events() -> None:
    result = classify_filing("S-3")
    assert result == {"kind": "Offering or dilution filing", "sentiment": "risk", "score": 82}
