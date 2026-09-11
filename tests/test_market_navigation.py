from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from runner_web import db
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


@pytest.mark.parametrize("active_tab", ["pulse", "radar", "alpha"])
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
    tabs = navigation.links["Memecoins board views"]
    assert [link["href"] for link in tabs] == [
        "/memecoins?view=pulse",
        "/memecoins?view=changed",
        "/memecoins?view=calls",
    ]
    assert tabs[["pulse", "radar", "alpha"].index(active_tab)]["aria-current"] == "page"
    assert 'class="memecoins-product"' in html


@pytest.mark.parametrize(
    ("product", "prefix", "label", "routes"),
    [
        (
            "runners",
            "/sports",
            "Stocks",
            ["/?view=pulse", "/?view=changed", "/?view=calls"],
        ),
        (
            "sports",
            "",
            "Sports",
            ["/?view=pulse", "/?view=changed", "/?view=calls"],
        ),
        (
            "sports",
            "/sports",
            "Sports",
            ["/sports/?view=pulse", "/sports/?view=changed", "/sports/?view=calls"],
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
    assert [link["href"] for link in navigation.links[f"{label} board views"]] == routes


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("pulse", "pulse"),
        ("changed", "changed"),
        ("calls", "calls"),
        ("radar", "pulse"),
        ("alpha", "pulse"),
        ("", "pulse"),
        (None, "pulse"),
    ],
)
def test_board_view_normalises_unknown_values(requested: str | None, expected: str) -> None:
    assert web_main.board_view(requested) == expected


def test_board_view_links_preserve_the_market() -> None:
    assert web_main._board_view_links("stocks", "/sports") == {
        "pulse": "/?view=pulse",
        "changed": "/?view=changed",
        "calls": "/?view=calls",
    }
    assert web_main._board_view_links("memecoins", "/sports") == {
        "pulse": "/memecoins?view=pulse",
        "changed": "/memecoins?view=changed",
        "calls": "/memecoins?view=calls",
    }
    assert web_main._board_view_links("sports", "") == {
        "pulse": "/?view=pulse",
        "changed": "/?view=changed",
        "calls": "/?view=calls",
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
        ("/", "Pulse · RATi Runners"),
        ("/?view=changed", "Changed · RATi Runners"),
        ("/?view=calls", "Calls · RATi Runners"),
        ("/?view=radar", "Pulse · RATi Runners"),
        ("/memecoins", "Memecoins · RATi Memecoins"),
        ("/memecoins?view=changed", "Changed · RATi Memecoins"),
        ("/memecoins?view=calls", "Calls · RATi Memecoins"),
    ],
)
def test_the_board_renders_every_view_on_one_screen(
    board_client: TestClient, path: str, title: str
) -> None:
    response = board_client.get(path)

    assert response.status_code == 200
    assert f"<title>{title}</title>" in response.text


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
