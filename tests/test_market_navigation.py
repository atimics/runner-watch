from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader


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


@pytest.mark.parametrize("host_product", ["runners", "sports"])
@pytest.mark.parametrize("active_tab", ["pulse", "radar", "alpha"])
def test_coin_navigation_keeps_market_context_on_each_host(host_product, active_tab) -> None:
    templates = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / "web/templates"))
    html = templates.get_template("mobile_base.html").render(
        product=host_product,
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
    tabs = navigation.links["Memecoins navigation"]
    assert [link["href"] for link in tabs] == [
        "/memecoins", "/memecoins/radar", "/memecoins/alpha"
    ]
    assert tabs[["pulse", "radar", "alpha"].index(active_tab)]["aria-current"] == "page"
    assert 'class="memecoins-product"' in html


@pytest.mark.parametrize(
    ("product", "prefix", "label", "routes"),
    [
        ("runners", "/sports", "Stocks", ["/", "/radar", "/community"]),
        ("sports", "", "Sports", ["/", "/radar", "/alpha"]),
        ("sports", "/sports", "Sports", ["/sports/", "/sports/radar", "/sports/alpha"]),
    ],
)
def test_existing_market_routes(product, prefix, label, routes) -> None:
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
    assert [link["href"] for link in navigation.links[f"{label} navigation"]] == routes
