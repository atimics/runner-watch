from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from runner_web import db, stories
from runner_web import main as web_main
from runner_web.db import init_db


class Navigation(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.nav = ""
        self.links: dict[str, list[dict[str, str | None]]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "nav":
            self.nav = values.get("aria-label") or ""
        elif tag == "a" and self.nav:
            self.links.setdefault(self.nav, []).append(values)

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav":
            self.nav = ""


@pytest.mark.parametrize("active_tab", ["pulse", "radar", "map", "alpha"])
def test_coin_navigation_keeps_market_context_on_each_host(active_tab: str) -> None:
    templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "web/templates"))
    html = templates.get_template("mobile_base.html").render(
        product="runners",
        nav_product="memecoins",
        active_tab=active_tab,
        user=None,
        runners_origin="https://runners.rati.chat",
        sports_origin="https://sports.rati.chat",
        request=SimpleNamespace(state=SimpleNamespace(csp_nonce="test")),
    )
    navigation = Navigation()
    navigation.feed(html)
    markets = navigation.links["Market"]
    assert [link["href"] for link in markets] == [
        "https://runners.rati.chat/",
        "https://runners.rati.chat/memecoins",
        "https://sports.rati.chat/",
    ]
    assert [link["href"] for link in markets if link.get("aria-current")] == [
        "https://runners.rati.chat/memecoins"
    ]
    tabs = navigation.links["View"]
    assert [link["href"] for link in tabs] == ["/memecoins?view=list", "/memecoins?view=map"]
    assert 'class="memecoins-product"' in html


@pytest.mark.parametrize(
    ("product", "prefix", "label", "routes"),
    [
        (
            "runners",
            "/sports",
            "Stocks",
            ["/?view=list", "/?view=map"],
        ),
        (
            "sports",
            "",
            "Sports",
            ["/?view=list", "/?view=map"],
        ),
        (
            "sports",
            "/sports",
            "Sports",
            ["/sports/?view=list", "/sports/?view=map"],
        ),
    ],
)
def test_board_view_links_share_one_screen(
    product: str, prefix: str, label: str, routes: list[str]
) -> None:
    templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "web/templates"))
    html = templates.get_template("mobile_base.html").render(
        product=product,
        sports_path_prefix=prefix,
        active_tab="radar",
        user=None,
        request=SimpleNamespace(state=SimpleNamespace(csp_nonce="test")),
    )
    navigation = Navigation()
    navigation.feed(html)
    assert [link["href"] for link in navigation.links["View"]] == routes


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("pulse", "pulse"),
        ("changed", "changed"),
        ("map", "map"),
        ("calls", "calls"),
        ("radar", "list"),
        ("alpha", "list"),
        ("", "list"),
        (None, "list"),
    ],
)
def test_board_view_normalises_unknown_values(requested: str | None, expected: str) -> None:
    assert web_main.board_view(requested) == expected


def test_board_view_links_preserve_the_market() -> None:
    for market, base in [("stocks", "/"), ("memecoins", "/memecoins"), ("sports", "/")]:
        assert web_main._board_view_links(market, "") == {
            "list": base + "?view=list",
            "map": base + "?view=map",
        }


def _assert_redirect(response, location: str) -> None:
    assert response.status_code == 307
    assert response.headers["location"] == location


@pytest.fixture
def board_client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "board.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    init_db()
    monkeypatch.setattr(web_main, "enforce_rate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_main, "current_user", lambda *_args, **_kwargs: None)
    client = TestClient(web_main.app)
    try:
        yield client
    finally:
        client.close()


@pytest.mark.parametrize(
    ("path", "title"),
    [
        ("/", "Stocks · RATi"),
        ("/?view=changed", "Stocks · RATi"),
        ("/?view=map", "Stocks · RATi"),
        ("/?view=calls", "Stocks · RATi"),
        ("/?view=radar", "Stocks · RATi"),
        ("/memecoins", "Memecoins · RATi"),
        ("/memecoins?view=changed", "Memecoins · RATi"),
        ("/memecoins?view=map", "Memecoins · RATi"),
        ("/memecoins?view=calls", "Memecoins · RATi"),
    ],
)
def test_the_board_renders_every_view_on_one_screen(
    board_client: TestClient, path: str, title: str
) -> None:
    response = board_client.get(path)

    assert response.status_code == 200
    assert f"<title>{title}</title>" in response.text


def test_stock_board_reuses_the_prebuilt_public_screen(board_client, monkeypatch) -> None:
    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    web_main.PUBLIC_SCREEN_DATA_REFRESHING.clear()
    monkeypatch.setattr(web_main, "shared_cache_get", lambda _key: None)
    monkeypatch.setattr(web_main, "shared_cache_set", lambda *_args: None)
    feed_calls = 0
    story_calls = 0

    def feed(*, offset=0, limit=50):
        nonlocal feed_calls
        feed_calls += 1
        assert offset == 0
        assert limit == 50
        return {
            "rows": [
                {
                    "ticker": "FAST",
                    "company": "Fast Company",
                    "price": 2.5,
                    "change_pct": 4.0,
                    "score": 60.0,
                }
            ],
            "has_more": False,
            "updated_at": "2026-09-19T12:00:00+00:00",
        }

    def story_rows(market: str, tickers: list[str]):
        nonlocal story_calls
        story_calls += 1
        assert market == "stocks"
        assert tickers == ["FAST"]
        return {}

    monkeypatch.setattr(web_main, "_public_pulse_data", feed)
    monkeypatch.setattr(stories, "stories_by_subject", story_rows)

    first = board_client.get("/")
    second = board_client.get("/")

    assert first.status_code == second.status_code == 200
    assert 'href="/t/FAST"' in second.text
    assert feed_calls == 1
    assert story_calls == 1
    assert ("stock-list", "public") in web_main.WORKER_OWNED_SCREENS
    assert any(
        scope == "stock-list" and identity == "public"
        for scope, identity, _builder, _ttl in web_main._public_screen_refreshers()
    )


def test_search_filters_the_cached_board_instead_of_rebuilding_it(
    board_client, monkeypatch
) -> None:
    """A query used to rebuild the whole pulse board and its stories; now only
    the first one does, and the rest filter the warm rows in memory."""

    web_main.PUBLIC_SCREEN_DATA_CACHE.clear()
    web_main.PUBLIC_SCREEN_DATA_REFRESHING.clear()
    monkeypatch.setattr(web_main, "shared_cache_get", lambda _key: None)
    monkeypatch.setattr(web_main, "shared_cache_set", lambda *_args: None)
    feed_calls = 0
    story_calls = 0

    def feed(*, offset=0, limit=50):
        nonlocal feed_calls
        feed_calls += 1
        return {
            "rows": [
                {"ticker": "FAST", "company": "Fast Company", "price": 2.5, "change_pct": 4.0},
                {"ticker": "SLOW", "company": "Slow Company", "price": 1.5, "change_pct": -1.0},
            ],
            "has_more": False,
            "updated_at": "2026-09-19T12:00:00+00:00",
        }

    def story_rows(market: str, tickers: list[str]):
        nonlocal story_calls
        story_calls += 1
        return {}

    monkeypatch.setattr(web_main, "_public_pulse_data", feed)
    monkeypatch.setattr(stories, "stories_by_subject", story_rows)

    first = board_client.get("/?q=fast")
    second = board_client.get("/?q=slow")

    assert first.status_code == second.status_code == 200
    assert 'href="/t/FAST"' in first.text and 'href="/t/SLOW"' not in first.text
    assert 'href="/t/SLOW"' in second.text
    assert feed_calls == 1
    assert story_calls == 1
    assert ("stock-search-base", "public") in web_main.WORKER_OWNED_SCREENS
    assert any(
        scope == "stock-search-base" and identity == "public"
        for scope, identity, _builder, _ttl in web_main._public_screen_refreshers()
    )


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/radar", "/?view=changed"),
        ("/alpha", "/?view=calls"),
        ("/community", "/?view=calls"),
        ("/memecoins/radar", "/memecoins?view=changed"),
        ("/memecoins/alpha", "/memecoins?view=calls"),
    ],
)
def test_retired_routes_redirect_through_the_app(
    board_client: TestClient, path: str, location: str
) -> None:
    response = board_client.get(path, follow_redirects=False)

    _assert_redirect(response, location)


def test_retired_routes_redirect_to_the_board() -> None:
    request = SimpleNamespace()
    _assert_redirect(web_main.radar_page(request, None), "/?view=changed")
    _assert_redirect(web_main.alpha_page(request, None), "/?view=calls")
    _assert_redirect(web_main.community_page(request, None), "/?view=calls")
    _assert_redirect(web_main.memecoins_radar_page(request, None), "/memecoins?view=changed")
    _assert_redirect(web_main.memecoin_alpha_redirect(request, None), "/memecoins?view=calls")
    _assert_redirect(
        web_main.sports_radar_page(request, None),
        f"{web_main.SPORTS_ORIGIN}/?view=changed",
    )
    _assert_redirect(
        web_main.sports_alpha_page(request, None),
        f"{web_main.SPORTS_ORIGIN}/?view=calls",
    )


def test_stock_search_reaches_rows_after_the_first_page(board_client, monkeypatch):
    pages = []

    def feed(*, offset=0, limit=50):
        pages.append(offset)
        if not offset:
            return {"rows": [{"ticker": "FIRST", "price": 1}], "has_more": True}
        return {
            "rows": [{"ticker": "FOUND", "company": "Found Company", "price": 2}],
            "has_more": False,
        }

    monkeypatch.setattr(web_main, "_public_pulse_data", feed)
    response = board_client.get("/?q=found")
    assert response.status_code == 200
    assert 'href="/t/FOUND"' in response.text
    assert 'href="/t/FIRST"' not in response.text
    assert pages == [0, 1]


def test_stock_search_opens_a_tracked_ticker_outside_the_pulse_board(
    board_client, monkeypatch
):
    monkeypatch.setattr(
        web_main,
        "_public_pulse_data",
        lambda **_: {"rows": [], "has_more": False, "updated_at": ""},
    )
    monkeypatch.setattr(web_main, "_ticker_exists", lambda ticker: ticker == "AAPL")
    monkeypatch.setattr(
        web_main,
        "_public_ticker_detail_data",
        lambda ticker: {
            "ticker": ticker,
            "company": "Apple Inc.",
            "current": {
                "ticker": ticker,
                "company": "Apple Inc.",
                "price": 213.45,
                "change_pct": 1.2,
                "score": 0,
            },
        },
    )

    response = board_client.get("/?q=aapl")

    assert response.status_code == 200
    assert 'href="/t/AAPL"' in response.text
    assert "Try another search" not in response.text


def test_screen_quote_and_chart_keep_provider_details_private(board_client, monkeypatch):
    secret = "private-provider-log"
    monkeypatch.setattr(web_main, "_known_ticker", lambda _: True)
    monkeypatch.setattr(web_main, "_ticker_exists", lambda _: True)
    monkeypatch.setattr(
        web_main,
        "ticker_quote",
        lambda _: {
            "price": 2.5,
            "change_pct": 3.2,
            "observed_at": "2026-09-12T12:00:00Z",
            "source": secret,
            "last_error": secret,
        },
    )
    monkeypatch.setattr(
        web_main,
        "ticker_chart_detail_payload",
        lambda _: {
            "points": [{"time": "2026-09-12T12:00:00Z", "close": 2.5, "source": secret}],
            "freshness": {"error": secret},
            "annotations": [secret],
        },
    )
    quote = board_client.get("/api/screens/stocks/ABC/quote").json()
    assert quote == {
        "value": "$2.50",
        "change": "+3.2%",
        "tone": "up",
        "time": "Sep 12 · 12:00 UTC",
    }
    chart = board_client.get("/api/screens/stocks/ABC/chart").json()
    assert chart["points"] == [{"value": 2.5, "time": "2026-09-12T12:00:00Z"}]
    # The state history carries when the action tag changed and nothing else -
    # no trade state, stage or rug level, which are what it is collapsed from.
    assert set(chart) == {"points", "states", "gap"}
    assert chart["gap"] is None  # no bars, so no projection to publish
    assert all(set(change) == {"time", "tone"} for change in chart["states"])
