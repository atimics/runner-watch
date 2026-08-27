from __future__ import annotations

import os
from dataclasses import dataclass


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _enabled_by_default(name: str) -> bool:
    return os.getenv(name, "true").strip().lower() not in {"0", "false", "no", "off"}


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
        feed="company_facts",
        title="SEC company facts",
        owner="U.S. Securities and Exchange Commission",
        terms_url="https://www.sec.gov/about/privacy-information#security",
        credential_env="SEC_USER_AGENT",
        expected_cadence_seconds=86_400,
        stale_after_seconds=172_800,
        schedule="rotation",
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
        source="yahoo",
        feed="news_search",
        title="Yahoo Finance ticker news search",
        owner="Yahoo",
        terms_url="https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html",
        credential_env=None,
        expected_cadence_seconds=900,
        stale_after_seconds=2_700,
        schedule="always",
        storage_policy="normalized_metadata_only",
        display_policy="review_required",
        attribution="Yahoo Finance",
        review_status="poc_only",
        enabled=(
            _enabled_by_default("DISCOVERY_SOURCES_ENABLED")
            and _enabled_by_default("YAHOO_NEWS_ENABLED")
        ),
    ),
    SourcePolicy(
        source="apewisdom",
        feed="reddit_trends",
        title="ApeWisdom Reddit stock trends",
        owner="ApeWisdom",
        terms_url="https://apewisdom.io/api/",
        credential_env=None,
        expected_cadence_seconds=900,
        stale_after_seconds=2_700,
        schedule="always",
        storage_policy="normalized_aggregates_only",
        display_policy="source_link_with_attribution",
        attribution="ApeWisdom / Reddit",
        review_status="poc_only",
        enabled=(
            _enabled_by_default("DISCOVERY_SOURCES_ENABLED")
            and _enabled_by_default("APEWISDOM_SOCIAL_ENABLED")
        ),
    ),
    SourcePolicy(
        source="gdelt",
        feed="news_search",
        title="GDELT company news search",
        owner="GDELT Project",
        terms_url="https://www.gdeltproject.org/about.html",
        credential_env=None,
        expected_cadence_seconds=900,
        stale_after_seconds=2_700,
        schedule="always",
        storage_policy="normalized_metadata_only",
        display_policy="source_link_with_attribution",
        attribution="GDELT",
        review_status="poc_only",
        enabled=(
            _enabled_by_default("DISCOVERY_SOURCES_ENABLED")
            and _enabled("GDELT_NEWS_ENABLED")
        ),
    ),
    SourcePolicy(
        source="bluesky",
        feed="social_search",
        title="Bluesky public cashtag search",
        owner="Bluesky",
        terms_url="https://bsky.social/about/support/tos",
        credential_env=None,
        expected_cadence_seconds=900,
        stale_after_seconds=2_700,
        schedule="always",
        storage_policy="normalized_aggregates_only",
        display_policy="source_link_with_attribution",
        attribution="Bluesky",
        review_status="poc_only",
        enabled=(
            _enabled_by_default("DISCOVERY_SOURCES_ENABLED")
            and _enabled("BLUESKY_SEARCH_ENABLED")
        ),
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
        display_policy="source_link_with_attribution",
        attribution="Nasdaq Trader",
        review_status="poc_only",
        enabled=_enabled("NASDAQ_TRADE_HALTS_ENABLED"),
    ),
    SourcePolicy(
        source="fintel",
        feed="short_interest",
        title="Fintel exchange-reported short interest",
        owner="Fintel Ventures LLC",
        terms_url="https://fintel.io/terms",
        credential_env="FINTEL_API_KEY",
        expected_cadence_seconds=900,
        stale_after_seconds=86_400,
        schedule="us_extended_weekdays",
        storage_policy="normalized_only",
        display_policy="source_link_with_attribution",
        attribution="Fintel",
        review_status="review_required",
        enabled=(
            _enabled_by_default("FINTEL_SHORT_DATA_ENABLED")
            and bool(os.getenv("FINTEL_API_KEY", "").strip())
        ),
    ),
    SourcePolicy(
        source="fintel",
        feed="borrow_rate",
        title="Fintel borrow fee and shares available",
        owner="Fintel Ventures LLC",
        terms_url="https://fintel.io/terms",
        credential_env="FINTEL_API_KEY",
        expected_cadence_seconds=900,
        stale_after_seconds=2_700,
        schedule="us_extended_weekdays",
        storage_policy="normalized_only",
        display_policy="source_link_with_attribution",
        attribution="Fintel",
        review_status="review_required",
        enabled=(
            _enabled_by_default("FINTEL_SHORT_DATA_ENABLED")
            and bool(os.getenv("FINTEL_API_KEY", "").strip())
        ),
    ),
    SourcePolicy(
        source="espn",
        feed="sports_scoreboard_preview",
        title="ESPN sports scoreboard preview",
        owner="ESPN",
        terms_url="https://disneytermsofuse.com/",
        credential_env=None,
        expected_cadence_seconds=600,
        stale_after_seconds=1_800,
        schedule="always",
        storage_policy="normalized_only",
        display_policy="preview_with_attribution",
        attribution="ESPN",
        review_status="poc_only",
        enabled=_enabled_by_default("SPORTS_INGESTION_ENABLED"),
    ),
    SourcePolicy(
        source="espn",
        feed="sports_boxscore_preview",
        title="ESPN sports box score preview",
        owner="ESPN",
        terms_url="https://disneytermsofuse.com/",
        credential_env=None,
        expected_cadence_seconds=600,
        stale_after_seconds=86_400,
        schedule="always",
        storage_policy="normalized_only",
        display_policy="preview_with_attribution",
        attribution="ESPN",
        review_status="poc_only",
        enabled=_enabled_by_default("SPORTS_INGESTION_ENABLED"),
    ),
    SourcePolicy(
        source="espn",
        feed="sports_news_preview",
        title="ESPN sports news preview",
        owner="ESPN",
        terms_url="https://disneytermsofuse.com/",
        credential_env=None,
        expected_cadence_seconds=600,
        stale_after_seconds=3_600,
        schedule="always",
        storage_policy="normalized_only",
        display_policy="preview_with_attribution",
        attribution="ESPN",
        review_status="poc_only",
        enabled=_enabled_by_default("SPORTS_INGESTION_ENABLED"),
    ),
)
