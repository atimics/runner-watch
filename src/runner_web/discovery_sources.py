from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from runner_watch.ingestion import MarketEvent, SourceBatch, SourceFetch
from runner_web.db import connection
from runner_web.ingestion import record_source_batch

USER_AGENT = "RunnerWatch/0.2 https://stonks.rati.foundation"
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
BLUESKY_BASE = os.getenv("BLUESKY_APPVIEW_BASE", "https://public.api.bsky.app").rstrip("/")
BLUESKY_SEARCH_URL = f"{BLUESKY_BASE}/xrpc/app.bsky.feed.searchPosts"
YAHOO_NEWS_URL = "https://query2.finance.yahoo.com/v1/finance/search"
APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
Download = Callable[[str, float], tuple[bytes, str | None]]

_COMPANY_SUFFIXES = {
    "class",
    "common",
    "company",
    "corp",
    "corporation",
    "holdings",
    "inc",
    "incorporated",
    "limited",
    "ltd",
    "plc",
    "stock",
}


def discovery_sources_enabled() -> bool:
    return os.getenv("DISCOVERY_SOURCES_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _source_enabled(name: str, default: bool) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() not in {"0", "false", "no", "off"}


def yahoo_news_enabled() -> bool:
    return discovery_sources_enabled() and _source_enabled("YAHOO_NEWS_ENABLED", True)


def apewisdom_social_enabled() -> bool:
    return discovery_sources_enabled() and _source_enabled("APEWISDOM_SOCIAL_ENABLED", True)


def gdelt_news_enabled() -> bool:
    return discovery_sources_enabled() and _source_enabled("GDELT_NEWS_ENABLED", False)


def bluesky_search_enabled() -> bool:
    return discovery_sources_enabled() and _source_enabled("BLUESKY_SEARCH_ENABLED", False)


def discovery_watchlist(limit: int = 30) -> list[dict[str, str]]:
    """Build a small POC watchlist: 10 Pulse leaders, 10 Alpha leaders, then flex."""

    limit = max(1, min(limit, 100))
    with connection() as database:
        latest_run = database.execute(
            """
            SELECT id FROM scan_runs WHERE candidate_rows>0
            ORDER BY captured_at DESC LIMIT 1
            """
        ).fetchone()
        scan_rows = (
            database.execute(
                """
                SELECT s.ticker,COALESCE(c.name,s.ticker) AS company
                FROM scan_snapshots s
                LEFT JOIN sec_companies c ON c.ticker=s.ticker
                WHERE s.scan_run_id=?
                ORDER BY COALESCE(s.baseline_rank,1000000),s.score DESC,s.ticker
                LIMIT 40
                """,
                (latest_run["id"],),
            ).fetchall()
            if latest_run
            else []
        )
        reaction_rows = database.execute(
            """
            SELECT r.ticker,COALESCE(c.name,r.ticker) AS company,COUNT(*) AS activity,
                   MAX(r.updated_at) AS latest_activity
            FROM ticker_reactions r
            LEFT JOIN sec_companies c ON c.ticker=r.ticker
            GROUP BY r.ticker,c.name
            ORDER BY activity DESC,latest_activity DESC,r.ticker
            LIMIT 20
            """
        ).fetchall()
        comment_rows = database.execute(
            """
            SELECT t.ticker,COALESCE(c.name,t.ticker) AS company,COUNT(*) AS activity,
                   MAX(t.created_at) AS latest_activity
            FROM ticker_comments t
            LEFT JOIN sec_companies c ON c.ticker=t.ticker
            WHERE t.status='public'
            GROUP BY t.ticker,c.name
            ORDER BY activity DESC,latest_activity DESC,t.ticker
            LIMIT 20
            """
        ).fetchall()
        filing_rows = database.execute(
            """
            SELECT f.ticker,COALESCE(MAX(c.name),MAX(f.company),f.ticker) AS company
            FROM sec_filings f
            LEFT JOIN sec_companies c ON c.ticker=f.ticker
            GROUP BY f.ticker
            ORDER BY MAX(f.filed_at) DESC LIMIT 30
            """
        ).fetchall()

    community: dict[str, dict[str, object]] = {}
    for row in reaction_rows:
        community[str(row["ticker"])] = {
            "ticker": row["ticker"],
            "company": row["company"],
            "activity": int(row["activity"]),
            "latest_activity": str(row["latest_activity"] or ""),
        }
    for row in comment_rows:
        ticker = str(row["ticker"])
        item = community.setdefault(
            ticker,
            {
                "ticker": row["ticker"],
                "company": row["company"],
                "activity": 0,
                "latest_activity": "",
            },
        )
        item["activity"] = int(item["activity"]) + (int(row["activity"]) * 2)
        item["latest_activity"] = max(
            str(item["latest_activity"]),
            str(row["latest_activity"] or ""),
        )
    leaders = list(scan_rows[:10])
    alpha = sorted(
        community.values(),
        key=lambda row: (int(row["activity"]), str(row["latest_activity"]), str(row["ticker"])),
        reverse=True,
    )[:10]
    flex = list(scan_rows[10:30])
    ordered = leaders + alpha + flex + list(filing_rows)
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in ordered:
        ticker = str(row["ticker"] or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        output.append({"ticker": ticker, "company": str(row["company"] or ticker).strip()})
        if len(output) >= limit:
            break
    return output


def _download(url: str, timeout: float) -> tuple[bytes, str | None]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read(), response.headers.get_content_type()


def _json_body(body: bytes) -> dict[str, Any]:
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Source response is not a JSON object")
    return parsed


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _company_terms(company: str) -> tuple[str, ...]:
    words = re.findall(r"[A-Za-z0-9]+", company.lower())
    return tuple(word for word in words if len(word) >= 3 and word not in _COMPANY_SUFFIXES)


def _strong_news_match(title: str, ticker: str, company: str) -> bool:
    lowered = title.lower()
    terms = _company_terms(company)
    if terms and any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in terms[:3]):
        return True
    cashtag = re.search(rf"(?<![A-Z0-9])\${re.escape(ticker)}\b", title, re.IGNORECASE)
    if cashtag:
        return True
    return len(ticker) >= 4 and bool(
        re.search(rf"\b{re.escape(ticker)}\b", title, re.IGNORECASE)
    )


def parse_gdelt_articles(
    payload: dict[str, Any],
    *,
    ticker: str,
    company: str,
    collected_at: datetime | None = None,
) -> tuple[MarketEvent, ...]:
    fallback_time = collected_at or datetime.now(UTC)
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return ()
    events: list[MarketEvent] = []
    seen_urls: set[str] = set()
    for raw in articles:
        if not isinstance(raw, dict):
            continue
        source_url = str(raw.get("url") or "").strip()
        title = " ".join(str(raw.get("title") or "").split())
        if not source_url or not title or source_url in seen_urls:
            continue
        if not _strong_news_match(title, ticker, company):
            continue
        seen_urls.add(source_url)
        published_at = _parse_time(raw.get("seendate")) or fallback_time
        event_id = hashlib.sha256(source_url.encode()).hexdigest()[:32]
        events.append(
            MarketEvent(
                event_id=event_id,
                version=event_id,
                ticker=ticker,
                event_type="news_article",
                event_at=published_at,
                published_at=published_at,
                status="published",
                source_url=source_url,
                payload={
                    "title": title[:300],
                    "domain": str(raw.get("domain") or "")[:120],
                    "language": str(raw.get("language") or "")[:40],
                    "source_country": str(raw.get("sourcecountry") or "")[:80],
                },
            )
        )
    return tuple(events)


def _gdelt_query(ticker: str, company: str) -> str:
    terms = _company_terms(company)
    if terms:
        phrase = " ".join(company.split()[:6]).replace('"', "")
        return f'"{phrase}" OR "${ticker}"'
    return f'"${ticker}" OR "{ticker} stock"'


def refresh_gdelt_news(
    ticker: str,
    company: str,
    *,
    timeout: float = 12,
    download: Download = _download,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    query = urllib.parse.urlencode(
        {
            "query": _gdelt_query(ticker, company),
            "mode": "artlist",
            "maxrecords": 20,
            "timespan": "1d",
            "sort": "datedesc",
            "format": "json",
        }
    )
    locator = f"{GDELT_URL}?{query}"
    try:
        body, content_type = download(locator, timeout)
        parsed = _json_body(body)
        events = parse_gdelt_articles(
            parsed,
            ticker=ticker,
            company=company,
            collected_at=datetime.now(UTC),
        )
    except Exception as exc:
        run_id = record_source_batch(
            SourceBatch(
                fetch=SourceFetch.failure(
                    source="gdelt",
                    feed="news_search",
                    locator=locator,
                    started_at=started_at,
                    error=exc,
                    metadata={"ticker": ticker, "requested_count": 1},
                )
            )
        )
        raise RuntimeError(f"GDELT news search failed in run {run_id}: {exc}") from exc
    article_count = len(parsed.get("articles") or [])
    fetch = SourceFetch.success(
        source="gdelt",
        feed="news_search",
        locator=locator,
        started_at=started_at,
        payload={"ticker": ticker, "articles_received": article_count, "matched": len(events)},
        content_type=content_type or "application/json",
        metadata={
            "ticker": ticker,
            "requested_count": 1,
            "received_count": len(events),
            "articles_received": article_count,
        },
    )
    run_id = record_source_batch(SourceBatch(fetch=fetch, market_events=events))
    return {"run_id": run_id, "events": len(events), "status": fetch.status}


def parse_yahoo_news(
    payload: dict[str, Any],
    *,
    ticker: str,
    collected_at: datetime | None = None,
) -> tuple[MarketEvent, ...]:
    fallback_time = collected_at or datetime.now(UTC)
    articles = payload.get("news")
    if not isinstance(articles, list):
        return ()
    events: list[MarketEvent] = []
    seen_ids: set[str] = set()
    for raw in articles:
        if not isinstance(raw, dict):
            continue
        related = [str(value).upper() for value in raw.get("relatedTickers") or []]
        if ticker not in related:
            continue
        source_url = str(raw.get("link") or "").strip()
        title = " ".join(str(raw.get("title") or "").split())
        article_id = str(raw.get("uuid") or "").strip()
        if not article_id:
            article_id = hashlib.sha256(source_url.encode()).hexdigest()[:32]
        if not source_url or not title or article_id in seen_ids:
            continue
        seen_ids.add(article_id)
        try:
            published_at = datetime.fromtimestamp(int(raw["providerPublishTime"]), tz=UTC)
        except (KeyError, TypeError, ValueError, OSError):
            published_at = fallback_time
        events.append(
            MarketEvent(
                event_id=article_id,
                version=article_id,
                ticker=ticker,
                event_type="news_article",
                event_at=published_at,
                published_at=published_at,
                status="published",
                source_url=source_url,
                payload={
                    "title": title[:300],
                    "publisher": str(raw.get("publisher") or "")[:120],
                    "article_type": str(raw.get("type") or "")[:40],
                    "related_tickers": related[:20],
                    "network_label": "Yahoo Finance",
                },
            )
        )
    return tuple(events)


def refresh_yahoo_news(
    ticker: str,
    company: str,
    *,
    timeout: float = 12,
    download: Download = _download,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    query = urllib.parse.urlencode({"q": ticker, "quotesCount": 1, "newsCount": 10})
    locator = f"{YAHOO_NEWS_URL}?{query}"
    try:
        body, content_type = download(locator, timeout)
        parsed = _json_body(body)
        events = parse_yahoo_news(parsed, ticker=ticker, collected_at=datetime.now(UTC))
    except Exception as exc:
        run_id = record_source_batch(
            SourceBatch(
                fetch=SourceFetch.failure(
                    source="yahoo",
                    feed="news_search",
                    locator=locator,
                    started_at=started_at,
                    error=exc,
                    metadata={"ticker": ticker, "company": company, "requested_count": 1},
                )
            )
        )
        raise RuntimeError(f"Yahoo news search failed in run {run_id}: {exc}") from exc
    article_count = len(parsed.get("news") or [])
    fetch = SourceFetch.success(
        source="yahoo",
        feed="news_search",
        locator=locator,
        started_at=started_at,
        payload={"ticker": ticker, "articles_received": article_count, "matched": len(events)},
        content_type=content_type or "application/json",
        metadata={
            "ticker": ticker,
            "company": company,
            "requested_count": 1,
            "received_count": len(events),
            "articles_received": article_count,
        },
    )
    run_id = record_source_batch(SourceBatch(fetch=fetch, market_events=events))
    return {"run_id": run_id, "events": len(events), "status": fetch.status}


def _post_url(post: dict[str, Any]) -> str | None:
    uri = str(post.get("uri") or "")
    handle = str((post.get("author") or {}).get("handle") or "")
    if not handle or "/" not in uri:
        return None
    record_key = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{urllib.parse.quote(handle)}/post/{record_key}"


def parse_bluesky_posts(
    payload: dict[str, Any],
    *,
    ticker: str,
    collected_at: datetime | None = None,
) -> tuple[MarketEvent, ...]:
    posts = payload.get("posts")
    if not isinstance(posts, list):
        return ()
    cashtag = re.compile(rf"(?<![A-Z0-9])\${re.escape(ticker)}\b", re.IGNORECASE)
    matched: list[dict[str, Any]] = []
    for raw in posts:
        if not isinstance(raw, dict):
            continue
        record = raw.get("record") if isinstance(raw.get("record"), dict) else {}
        if not cashtag.search(str(record.get("text") or "")):
            continue
        matched.append(raw)
    if not matched:
        return ()

    mention_count = len(matched)
    like_count = sum(int(post.get("likeCount") or 0) for post in matched)
    repost_count = sum(int(post.get("repostCount") or 0) for post in matched)
    reply_count = sum(int(post.get("replyCount") or 0) for post in matched)
    engagement_count = like_count + 2 * repost_count + reply_count
    if mention_count < 3 and engagement_count < 10:
        return ()

    event_times = [
        stamp
        for post in matched
        if (
            stamp := _parse_time(
                (post.get("record") or {}).get("createdAt") or post.get("indexedAt")
            )
        )
    ]
    event_at = max(event_times, default=collected_at or datetime.now(UTC))
    bucket_minute = event_at.minute - event_at.minute % 15
    bucket = event_at.replace(minute=bucket_minute, second=0, microsecond=0)
    authors = {
        str((post.get("author") or {}).get("handle") or "")
        for post in matched
        if (post.get("author") or {}).get("handle")
    }
    sample_urls = [url for post in matched if (url := _post_url(post))][:3]
    source_url = sample_urls[0] if sample_urls else "https://bsky.app/search?q=%24" + ticker
    return (
        MarketEvent(
            event_id=f"{ticker}:{bucket.isoformat()}",
            version=bucket.isoformat(),
            ticker=ticker,
            event_type="social_spike",
            event_at=event_at,
            published_at=event_at,
            status="active",
            source_url=source_url,
            payload={
                "mention_count": mention_count,
                "unique_authors": len(authors),
                "like_count": like_count,
                "repost_count": repost_count,
                "reply_count": reply_count,
                "engagement_count": engagement_count,
                "sample_urls": sample_urls,
                "network_label": "Bluesky",
            },
        ),
    )


def refresh_bluesky_social(
    ticker: str,
    *,
    timeout: float = 12,
    download: Download = _download,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    query = urllib.parse.urlencode({"q": f"${ticker}", "limit": 50, "sort": "latest"})
    locator = f"{BLUESKY_SEARCH_URL}?{query}"
    try:
        body, content_type = download(locator, timeout)
        parsed = _json_body(body)
        events = parse_bluesky_posts(parsed, ticker=ticker, collected_at=datetime.now(UTC))
    except Exception as exc:
        run_id = record_source_batch(
            SourceBatch(
                fetch=SourceFetch.failure(
                    source="bluesky",
                    feed="social_search",
                    locator=locator,
                    started_at=started_at,
                    error=exc,
                    metadata={"ticker": ticker, "requested_count": 1},
                )
            )
        )
        raise RuntimeError(f"Bluesky social search failed in run {run_id}: {exc}") from exc
    post_count = len(parsed.get("posts") or [])
    fetch = SourceFetch.success(
        source="bluesky",
        feed="social_search",
        locator=locator,
        started_at=started_at,
        payload={"ticker": ticker, "posts_received": post_count, "spikes": len(events)},
        content_type=content_type or "application/json",
        metadata={
            "ticker": ticker,
            "requested_count": 1,
            "received_count": len(events),
            "posts_received": post_count,
        },
    )
    run_id = record_source_batch(SourceBatch(fetch=fetch, market_events=events))
    return {"run_id": run_id, "events": len(events), "status": fetch.status}


def parse_apewisdom_social(
    payload: dict[str, Any],
    *,
    watched_tickers: set[str],
    collected_at: datetime | None = None,
) -> tuple[MarketEvent, ...]:
    observed_at = collected_at or datetime.now(UTC)
    results = payload.get("results")
    if not isinstance(results, list):
        return ()
    events: list[MarketEvent] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").strip().upper()
        if ticker not in watched_tickers:
            continue
        mentions = int(raw.get("mentions") or 0)
        prior_mentions = int(raw.get("mentions_24h_ago") or 0)
        mention_change = mentions - prior_mentions
        upvotes = int(raw.get("upvotes") or 0)
        if mentions < 5 or (mentions < 20 and mention_change < 3):
            continue
        event_day = observed_at.date().isoformat()
        events.append(
            MarketEvent(
                event_id=f"{ticker}:{event_day}",
                version=event_day,
                ticker=ticker,
                event_type="social_spike",
                event_at=observed_at,
                published_at=observed_at,
                status="active",
                source_url=f"https://apewisdom.io/stocks/{urllib.parse.quote(ticker)}/",
                payload={
                    "mention_count": mentions,
                    "engagement_count": upvotes,
                    "previous_mentions": prior_mentions,
                    "mention_change_24h": mention_change,
                    "rank": int(raw.get("rank") or 0),
                    "rank_24h_ago": int(raw.get("rank_24h_ago") or 0),
                    "network_label": "Reddit",
                },
            )
        )
    return tuple(events)


def refresh_apewisdom_social(
    watchlist: list[dict[str, str]],
    *,
    timeout: float = 12,
    download: Download = _download,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    watched_tickers = {str(row.get("ticker") or "").upper() for row in watchlist}
    try:
        body, content_type = download(APEWISDOM_URL, timeout)
        parsed = _json_body(body)
        events = parse_apewisdom_social(
            parsed,
            watched_tickers=watched_tickers,
            collected_at=datetime.now(UTC),
        )
    except Exception as exc:
        run_id = record_source_batch(
            SourceBatch(
                fetch=SourceFetch.failure(
                    source="apewisdom",
                    feed="reddit_trends",
                    locator=APEWISDOM_URL,
                    started_at=started_at,
                    error=exc,
                    metadata={"requested_count": len(watched_tickers)},
                )
            )
        )
        raise RuntimeError(f"ApeWisdom social refresh failed in run {run_id}: {exc}") from exc
    result_count = len(parsed.get("results") or [])
    fetch = SourceFetch.success(
        source="apewisdom",
        feed="reddit_trends",
        locator=APEWISDOM_URL,
        started_at=started_at,
        payload={
            "watched_tickers": len(watched_tickers),
            "results_received": result_count,
            "matched_spikes": len(events),
        },
        content_type=content_type or "application/json",
        metadata={
            "requested_count": len(watched_tickers),
            "received_count": len(events),
            "results_received": result_count,
        },
    )
    run_id = record_source_batch(SourceBatch(fetch=fetch, market_events=events))
    return {"run_id": run_id, "events": len(events), "status": fetch.status}
