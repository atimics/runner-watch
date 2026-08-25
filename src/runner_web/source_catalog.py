from __future__ import annotations

import os
from dataclasses import dataclass


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class SourcePolicy:
    """Storage, display, and freshness rules for one outside feed."""

    source: str
    feed: str
    title: str
    owner: str
    terms_url: str | None
    credential_env: str | None
    expected_cadence_seconds: int | None
    stale_after_seconds: int | None
    schedule: str
    storage_policy: str
    display_policy: str
    attribution: str | None
    review_status: str = "approved"
    enabled: bool = True


DEFAULT_SOURCE_POLICIES = (
    SourcePolicy(
        source="sec",
        feed="company_map",
        title="SEC listed company map",
        owner="U.S. Securities and Exchange Commission",
        terms_url="https://www.sec.gov/about/privacy-information#security",
        credential_env="SEC_USER_AGENT",
        expected_cadence_seconds=86_400,
        stale_after_seconds=172_800,
        schedule="always",
        storage_policy="archive_raw_and_normalized",
        display_policy="source_link_with_attribution",
        attribution="SEC",
    ),
    SourcePolicy(
        source="sec",
        feed="current_filings",
        title="SEC current filings",
        owner="U.S. Securities and Exchange Commission",
        terms_url="https://www.sec.gov/about/privacy-information#security",
        credential_env="SEC_USER_AGENT",
        expected_cadence_seconds=45,
        stale_after_seconds=180,
        schedule="always",
        storage_policy="archive_raw_and_normalized",
        display_policy="source_link_with_attribution",
        attribution="SEC",
    ),
    SourcePolicy(
        source="sec",
        feed="filing_index",
        title="SEC filing index",
        owner="U.S. Securities and Exchange Commission",
        terms_url="https://www.sec.gov/about/privacy-information#security",
        credential_env="SEC_USER_AGENT",
        expected_cadence_seconds=None,
        stale_after_seconds=None,
        schedule="event",
        storage_policy="archive_raw_and_normalized",
        display_policy="source_link_with_attribution",
        attribution="SEC",
    ),
    SourcePolicy(
        source="sec",
        feed="filing_document",
        title="SEC filing document",
        owner="U.S. Securities and Exchange Commission",
        terms_url="https://www.sec.gov/about/privacy-information#security",
        credential_env="SEC_USER_AGENT",
        expected_cadence_seconds=None,
        stale_after_seconds=None,
        schedule="event",
        storage_policy="archive_raw_and_normalized",
        display_policy="source_link_with_attribution",
        attribution="SEC",
    ),
    SourcePolicy(
        source="sec",
        feed="document",
        title="SEC source document",
        owner="U.S. Securities and Exchange Commission",
        terms_url="https://www.sec.gov/about/privacy-information#security",
        credential_env="SEC_USER_AGENT",
        expected_cadence_seconds=None,
        stale_after_seconds=None,
        schedule="event",
        storage_policy="archive_raw",
        display_policy="source_link_with_attribution",
        attribution="SEC",
    ),
    SourcePolicy(
        source="yahoo",
        feed="universe",
        title="Yahoo market universe",
        owner="Yahoo",
        terms_url="https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html",
        credential_env=None,
        expected_cadence_seconds=180,
        stale_after_seconds=900,
        schedule="us_extended_weekdays",
        storage_policy="normalized_only",
        display_policy="review_required",
        attribution="Yahoo Finance",
        review_status="review_required",
    ),
    SourcePolicy(
        source="yahoo",
        feed="market_bars",
        title="Yahoo price bars",
        owner="Yahoo",
        terms_url="https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html",
        credential_env=None,
        expected_cadence_seconds=180,
        stale_after_seconds=900,
        schedule="us_extended_weekdays",
        storage_policy="normalized_only",
        display_policy="review_required",
        attribution="Yahoo Finance",
        review_status="review_required",
    ),
    SourcePolicy(
        source="nasdaq_trader",
        feed="trade_halts",
        title="Nasdaq Trader trading halts",
        owner="Nasdaq",
        terms_url=(
            "https://www.nasdaqtrader.com/content/administrationsupport/"
            "agreementstrading/THRSSFeedTermsCond.pdf"
        ),
        credential_env=None,
        expected_cadence_seconds=60,
        stale_after_seconds=180,
        schedule="us_extended_weekdays",
        storage_policy="archive_raw_and_normalized",
        display_policy="shadow_only",
        attribution="Nasdaq Trader",
        review_status="review_required",
        enabled=_enabled("NASDAQ_TRADE_HALTS_ENABLED"),
    ),
)
