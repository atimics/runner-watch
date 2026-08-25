from datetime import UTC, datetime

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
    facts = parse_company_facts(
        payload, collected_at=datetime(2026, 8, 25, tzinfo=UTC)
    )
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
