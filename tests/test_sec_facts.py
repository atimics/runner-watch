from datetime import UTC, date, datetime

from runner_web.issuer_risk import build_issuer_risk_context
from runner_web.sec_facts import parse_company_facts


def test_company_facts_normalize_treasury_burn_and_share_supply() -> None:
    payload = {
        "cik": 22,
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": [
                            {
                                "val": 20_000_000,
                                "end": "2026-06-30",
                                "filed": "2026-08-01",
                                "accn": "new",
                                "form": "10-Q",
                            },
                            {
                                "val": 10_000_000,
                                "end": "2025-06-30",
                                "filed": "2025-08-01",
                                "accn": "old",
                                "form": "10-Q",
                            },
                        ]
                    }
                }
            },
            "us-gaap": {
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {
                        "USD": [
                            {
                                "val": 3_000_000,
                                "end": "2026-06-30",
                                "filed": "2026-08-01",
                                "accn": "cash",
                                "form": "10-Q",
                            }
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            {
                                "val": -6_000_000,
                                "start": "2026-01-01",
                                "end": "2026-06-30",
                                "filed": "2026-08-01",
                                "accn": "burn",
                                "form": "10-Q",
                            }
                        ]
                    }
                },
            },
        },
    }
    facts = parse_company_facts(payload, collected_at=datetime(2026, 8, 25, tzinfo=UTC))
    rows = [
        {
            "concept": fact.concept,
            "value": fact.value,
            "period_start": fact.period_start.isoformat() if fact.period_start else None,
            "period_end": fact.period_end.isoformat(),
            "filed_at": fact.filed_at.isoformat(),
        }
        for fact in facts
    ]
    context = build_issuer_risk_context(rows)
    assert context["issuer_data_available"] is True
    assert context["shares_growth_pct"] == 100.0
    assert 2.9 <= context["cash_runway_months"] <= 3.1


def test_missing_company_facts_stay_unknown() -> None:
    context = build_issuer_risk_context([])
    assert context["issuer_data_available"] is False
    assert context["cash_runway_months"] is None


def _burning_rows() -> list[dict]:
    # $1M of cash against $3M of operating outflow in a quarter: about a month.
    return [
        {
            "concept": "cash",
            "value": 1_000_000,
            "period_start": None,
            "period_end": "2026-06-30",
            "filed_at": "2026-08-05",
            "form": "10-Q",
        },
        {
            "concept": "operating_cash_flow",
            "value": -3_000_000,
            "period_start": "2026-04-01",
            "period_end": "2026-06-30",
            "filed_at": "2026-08-05",
            "form": "10-Q",
        },
    ]


def test_runway_does_not_apply_to_a_financial_company() -> None:
    # Live case: RWT, a REIT (SIC 6798), read about a month of runway because
    # its lending runs through operating cash flow.
    lender = build_issuer_risk_context(_burning_rows(), sic="6798")
    maker = build_issuer_risk_context(_burning_rows(), sic="3585")

    assert lender["financial"] is True and lender["runway_applies"] is False
    assert lender["cash_runway_months"] is None
    assert maker["runway_applies"] is True and maker["cash_runway_months"] < 3


def test_the_risk_check_no_longer_calls_a_lender_a_raise_risk() -> None:
    from runner_watch.risk import RiskInput, assess_risk

    def reasons(sic: str) -> list[str]:
        context = build_issuer_risk_context(_burning_rows(), sic=sic)
        item = RiskInput(
            setup_score=70.0,
            price=5.0,
            change_pct=3.0,
            momentum_5m_pct=1.0,
            momentum_15m_pct=2.0,
            vwap_position_pct=1.0,
            pullback_from_high_pct=1.0,
            close_location=0.8,
            dollar_volume=5_000_000,
            recent_dollar_volume=500_000,
            stale_minutes=2.0,
            cash_runway_months=context["cash_runway_months"],
            issuer_data_available=True,
        )
        return assess_risk(item).risk_reasons

    runway = "treasury runway is under 3 months; raise risk is critical"
    assert runway in reasons("3585")
    assert runway not in reasons("6798")


def test_an_ifrs_filer_is_read_in_its_own_currency() -> None:
    # Live shape: BLDP files a 40-F with ifrs-full facts in its reporting currency.
    def entry(val, end, accn, start=None):
        item = {"val": val, "end": end, "filed": "2026-03-10", "accn": accn, "form": "40-F"}
        return {**item, "start": start} if start else item

    payload = {
        "cik": 1453015,
        "facts": {
            "ifrs-full": {
                "CashAndCashEquivalents": {
                    "units": {"CAD": [entry(90_000_000, "2025-12-31", "a")]}
                },
                "CashFlowsFromUsedInOperatingActivities": {
                    "units": {"CAD": [entry(-60_000_000, "2025-12-31", "a", start="2025-01-01")]}
                },
                "NumberOfSharesOutstanding": {
                    "units": {
                        "shares": [
                            entry(300_000_000, "2025-12-31", "a"),
                            {**entry(250_000_000, "2024-12-31", "b"), "filed": "2025-03-10"},
                        ]
                    }
                },
            },
            # A currency outside IFRS facts is still not read.
            "us-gaap": {
                "CashAndCashEquivalentsAtCarryingValue": {
                    "units": {"EUR": [entry(1, "2025-12-31", "c")]}
                }
            },
        },
    }

    facts = parse_company_facts(payload, collected_at=datetime(2026, 9, 28, tzinfo=UTC))
    rows = [
        {
            "concept": fact.concept,
            "value": fact.value,
            "unit": fact.unit,
            "period_start": fact.period_start.isoformat() if fact.period_start else None,
            "period_end": fact.period_end.isoformat(),
            "filed_at": fact.filed_at.isoformat(),
            "form": fact.form,
        }
        for fact in facts
    ]
    context = build_issuer_risk_context(rows, sic="3690")

    assert {fact.source_tag for fact in facts} == {
        "ifrs-full:CashAndCashEquivalents",
        "ifrs-full:CashFlowsFromUsedInOperatingActivities",
        "ifrs-full:NumberOfSharesOutstanding",
    }
    assert 17.9 <= context["cash_runway_months"] <= 18.1  # CAD over CAD
    assert context["shares_growth_pct"] == 20.0
    assert context["reporting_currency"] == "CAD"
    assert context["cash"] is None  # never shown as dollars


def _share_row(value, end, tag, *, form="20-F", unit="shares", filed=None):
    return {
        "concept": "shares_outstanding",
        "value": value,
        "unit": unit,
        "period_start": None,
        "period_end": end,
        "filed_at": filed or f"{end}T00:00:00+00:00",
        "form": form,
        "source_tag": tag,
    }


COVER = "dei:EntityCommonStockSharesOutstanding"
NOTE = "ifrs-full:NumberOfSharesOutstanding"


def test_foreign_share_growth_is_read_on_one_basis() -> None:
    rows = [
        _share_row(200.0, "2025-12-31", NOTE),
        _share_row(100.0, "2024-12-31", NOTE),
    ]
    assert build_issuer_risk_context(rows)["shares_growth_pct"] == 100.0


def test_foreign_share_growth_across_two_tags_is_not_checked() -> None:
    # The latest fact is on the cover page, the older one is an IFRS note. If one
    # counts ADSs and the other ordinary shares, a jump would be false.
    rows = [
        _share_row(2_000.0, "2025-12-31", COVER),
        _share_row(100.0, "2024-12-31", NOTE),
    ]
    context = build_issuer_risk_context(rows)
    assert context["shares_growth_pct"] is None

    # The standard reads "not checked yet" (None), never "not met" (False).
    from ratitrust.stock import standards

    result = standards(
        exchange="Nasdaq",
        issuer=context,
        halted_on=None,
        delisting_on=None,
        today=date(2026, 9, 27),
    )
    dilution = next(item for item in result["standards"] if item["key"] == "dilution")
    assert dilution["met"] is None


def test_domestic_share_growth_is_unchanged_across_tags() -> None:
    rows = [
        _share_row(2_000.0, "2025-12-31", COVER, form="10-K"),
        _share_row(100.0, "2024-12-31", "us-gaap:CommonStockSharesOutstanding", form="10-K"),
    ]
    assert build_issuer_risk_context(rows)["shares_growth_pct"] == 1900.0


def test_rows_without_a_tag_still_give_the_old_domestic_answer() -> None:
    rows = [
        {**_share_row(20.0, "2026-06-30", None, form="10-Q"), "source_tag": None},
        {**_share_row(10.0, "2025-06-30", None, form="10-Q"), "source_tag": None},
    ]
    assert build_issuer_risk_context(rows)["shares_growth_pct"] == 100.0


def _money_row(concept, value, unit, form="20-F", start=None):
    return {
        "concept": concept,
        "value": value,
        "unit": unit,
        "period_start": start,
        "period_end": "2025-12-31",
        "filed_at": "2026-03-10T00:00:00+00:00",
        "form": form,
        "source_tag": "ifrs-full:x",
    }


def test_non_dollar_cash_is_shown_with_its_currency_code_and_not_converted() -> None:
    rows = [
        _money_row("cash", 1_200_000.0, "EUR"),
        _money_row("operating_cash_flow", -600_000.0, "EUR", start="2025-01-01"),
    ]
    context = build_issuer_risk_context(rows, sic="3690")
    assert context["cash"] is None
    assert context["cash_display"] == "EUR 1,200,000.00"
    assert context["reporting_currency"] == "EUR"
    assert 23.9 <= context["cash_runway_months"] <= 24.5


def test_dollar_cash_has_no_extra_key() -> None:
    rows = [_money_row("cash", 3_000_000.0, "USD", form="10-K")]
    context = build_issuer_risk_context(rows)
    assert context["cash"] == 3_000_000.0
    assert "cash_display" not in context


def test_runway_and_ratios_are_not_computed_across_currencies() -> None:
    # Burn is in EUR but the only cash is in USD, and debt is in USD.
    rows = [
        _money_row("cash", 5_000_000.0, "USD"),
        _money_row("debt_total", 1_000_000.0, "USD"),
        _money_row("operating_cash_flow", -600_000.0, "EUR", start="2025-01-01"),
    ]
    context = build_issuer_risk_context(rows, sic="3690")
    assert context["cash"] is None
    assert "cash_display" not in context
    assert context["cash_runway_months"] is None
    assert context["debt_to_cash"] is None
