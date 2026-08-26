import json
from datetime import UTC, datetime
from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import connection, init_db
from runner_web.discovery_sources import (
    discovery_watchlist,
    parse_apewisdom_social,
    parse_bluesky_posts,
    parse_gdelt_articles,
    parse_yahoo_news,
    refresh_apewisdom_social,
    refresh_bluesky_social,
    refresh_gdelt_news,
    refresh_yahoo_news,
)


def test_gdelt_parser_keeps_strong_company_matches_and_source_links() -> None:
    payload = {
        "articles": [
            {
                "url": "https://news.example/pen-launch",
                "title": "Penny Labs announces a new launch",
                "seendate": "20260825T173000Z",
                "domain": "news.example",
                "language": "English",
                "sourcecountry": "United States",
            },
            {
                "url": "https://news.example/noise",
                "title": "Someone picked up a pen",
                "seendate": "20260825T174000Z",
            },
        ]
    }

    events = parse_gdelt_articles(payload, ticker="PEN", company="Penny Labs Inc")

    assert len(events) == 1
    assert events[0].event_type == "news_article"
    assert events[0].source_url == "https://news.example/pen-launch"
    assert events[0].event_at == datetime(2026, 8, 25, 17, 30, tzinfo=UTC)
    assert events[0].payload["title"] == "Penny Labs announces a new launch"


def test_bluesky_parser_aggregates_cashtags_without_storing_post_text() -> None:
    posts = []
    for index in range(3):
        posts.append(
            {
                "uri": f"at://did:plc:test/app.bsky.feed.post/post{index}",
                "indexedAt": f"2026-08-25T17:0{index}:00Z",
                "record": {
                    "text": f"Watching $PEN today {index}",
                    "createdAt": f"2026-08-25T17:0{index}:00Z",
                },
                "author": {"handle": f"person{index}.bsky.social"},
                "likeCount": index + 1,
                "repostCount": 1,
                "replyCount": 0,
            }
        )
    posts.append(
        {
            "uri": "at://did:plc:test/app.bsky.feed.post/noise",
            "indexedAt": "2026-08-25T17:03:00Z",
            "record": {"text": "A plain pen is not a ticker"},
            "author": {"handle": "noise.bsky.social"},
        }
    )

    events = parse_bluesky_posts({"posts": posts}, ticker="PEN")

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "social_spike"
    assert event.payload["mention_count"] == 3
    assert event.payload["unique_authors"] == 3
    assert event.payload["engagement_count"] == 12
    assert event.payload["network_label"] == "Bluesky"
    assert "text" not in event.payload
    assert event.source_url.endswith("/post/post0")


def test_weak_bluesky_result_is_not_published() -> None:
    payload = {
        "posts": [
            {
                "uri": "at://did:plc:test/app.bsky.feed.post/one",
                "indexedAt": "2026-08-25T17:00:00Z",
                "record": {"text": "$PEN", "createdAt": "2026-08-25T17:00:00Z"},
                "author": {"handle": "quiet.bsky.social"},
                "likeCount": 1,
            }
        ]
    }

    assert parse_bluesky_posts(payload, ticker="PEN") == ()


def test_yahoo_news_requires_the_ticker_in_related_tickers() -> None:
    payload = {
        "news": [
            {
                "uuid": "matched",
                "title": "Penny Labs wins a contract",
                "publisher": "Example News",
                "link": "https://finance.yahoo.com/m/matched",
                "providerPublishTime": 1787672760,
                "relatedTickers": ["PEN"],
                "type": "STORY",
            },
            {
                "uuid": "noise",
                "title": "Another company wins",
                "link": "https://finance.yahoo.com/m/noise",
                "providerPublishTime": 1787672760,
                "relatedTickers": ["OTHER"],
            },
        ]
    }

    events = parse_yahoo_news(payload, ticker="PEN")

    assert len(events) == 1
    assert events[0].event_id == "matched"
    assert events[0].payload["publisher"] == "Example News"


def test_apewisdom_parser_keeps_meaningful_reddit_growth_for_watched_tickers() -> None:
    payload = {
        "results": [
            {
                "rank": 5,
                "ticker": "PEN",
                "mentions": 12,
                "upvotes": 44,
                "rank_24h_ago": 20,
                "mentions_24h_ago": 5,
            },
            {
                "rank": 8,
                "ticker": "QUIET",
                "mentions": 6,
                "upvotes": 2,
                "rank_24h_ago": 7,
                "mentions_24h_ago": 6,
            },
        ]
    }

    events = parse_apewisdom_social(
        payload,
        watched_tickers={"PEN", "QUIET"},
        collected_at=datetime(2026, 8, 25, 17, tzinfo=UTC),
    )

    assert len(events) == 1
    assert events[0].ticker == "PEN"
    assert events[0].payload["mention_change_24h"] == 7
    assert events[0].payload["network_label"] == "Reddit"


def test_refreshes_record_source_runs_and_normalized_events(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "discovery.db")
    init_db()
    gdelt_body = json.dumps(
        {
            "articles": [
                {
                    "url": "https://news.example/pen",
                    "title": "Penny Labs files an update",
                    "seendate": "20260825T173000Z",
                    "domain": "news.example",
                }
            ]
        }
    ).encode()
    bluesky_body = json.dumps(
        {
            "posts": [
                {
                    "uri": f"at://did:plc:test/app.bsky.feed.post/{index}",
                    "record": {
                        "text": f"$PEN update {index}",
                        "createdAt": f"2026-08-25T17:0{index}:00Z",
                    },
                    "author": {"handle": f"user{index}.bsky.social"},
                    "likeCount": 4,
                }
                for index in range(3)
            ]
        }
    ).encode()
    yahoo_body = json.dumps(
        {
            "news": [
                {
                    "uuid": "yahoo-pen",
                    "title": "Penny Labs releases an update",
                    "publisher": "Example News",
                    "link": "https://finance.yahoo.com/m/yahoo-pen",
                    "providerPublishTime": 1787672760,
                    "relatedTickers": ["PEN"],
                    "type": "STORY",
                }
            ]
        }
    ).encode()
    apewisdom_body = json.dumps(
        {
            "results": [
                {
                    "rank": 4,
                    "ticker": "PEN",
                    "mentions": 14,
                    "upvotes": 55,
                    "rank_24h_ago": 20,
                    "mentions_24h_ago": 4,
                }
            ]
        }
    ).encode()

    news = refresh_gdelt_news(
        "PEN",
        "Penny Labs Inc",
        download=lambda _url, _timeout: (gdelt_body, "application/json"),
    )
    social = refresh_bluesky_social(
        "PEN",
        download=lambda _url, _timeout: (bluesky_body, "application/json"),
    )
    yahoo = refresh_yahoo_news(
        "PEN",
        "Penny Labs Inc",
        download=lambda _url, _timeout: (yahoo_body, "application/json"),
    )
    reddit = refresh_apewisdom_social(
        [{"ticker": "PEN", "company": "Penny Labs Inc"}],
        download=lambda _url, _timeout: (apewisdom_body, "application/json"),
    )

    with connection() as database:
        runs = database.execute(
            "SELECT source,feed,status,received_count FROM ingestion_runs ORDER BY source"
        ).fetchall()
        events = database.execute(
            "SELECT source,event_type,ticker FROM market_events ORDER BY source"
        ).fetchall()
        documents = database.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
    assert news["events"] == 1
    assert social["events"] == 1
    assert yahoo["events"] == 1
    assert reddit["events"] == 1
    assert [tuple(row) for row in runs] == [
        ("apewisdom", "reddit_trends", "success", 1),
        ("bluesky", "social_search", "success", 1),
        ("gdelt", "news_search", "success", 1),
        ("yahoo", "news_search", "success", 1),
    ]
    assert [tuple(row) for row in events] == [
        ("apewisdom", "social_spike", "PEN"),
        ("bluesky", "social_spike", "PEN"),
        ("gdelt", "news_article", "PEN"),
        ("yahoo", "news_article", "PEN"),
    ]
    assert documents == 0


def test_watchlist_prioritizes_pulse_then_alpha_then_flex(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "watchlist.db")
    init_db()
    captured_at = datetime.now(UTC).isoformat()
    with connection() as database:
        database.execute(
            """
            INSERT INTO scan_runs(
                id,mode,label,feature_schema_version,requested_symbols,liquid_symbols,
                scanned_symbols,candidate_rows,failed_symbols_json,warnings_json,
                started_at,finished_at,captured_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "run", "penny", "Penny stocks", "test", 20, 20, 20, 12,
                "[]", "[]", captured_at, captured_at, captured_at,
            ),
        )
        for rank in range(1, 13):
            ticker = f"P{rank:02d}"
            database.execute(
                """
                INSERT INTO scan_snapshots(
                    id,ticker,score,stage,session,price,change_pct,momentum_5m_pct,
                    momentum_15m_pct,breakout_pct,dollar_volume,quote_time,
                    signals_json,risks_json,captured_at,scan_run_id,baseline_rank
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"snap-{rank}", ticker, 100-rank, "BUILDING", "regular", 1.0,
                    5.0, 1.0, 2.0, 0.5, 500_000, captured_at, "[]", "[]",
                    captured_at, "run", rank,
                ),
            )
        database.execute(
            """
            INSERT INTO ticker_reactions(profile_id,ticker,reaction,created_at,updated_at)
            VALUES(?,?,?,?,?)
            """,
            ("v:fan", "ALPHA", "bull", captured_at, captured_at),
        )

    watchlist = discovery_watchlist(13)

    assert [row["ticker"] for row in watchlist[:10]] == [f"P{rank:02d}" for rank in range(1, 11)]
    assert watchlist[10]["ticker"] == "ALPHA"
    assert [row["ticker"] for row in watchlist[11:]] == ["P11", "P12"]
