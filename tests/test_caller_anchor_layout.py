"""Caller records keep entries and outcomes readable on narrow screens."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).parents[1]
ADDRESS = "D9FLtSbZrQ4QoBP7izmnAZAkLFsdQt77vgo4rEL4qxGq"


def caller_html(*, calls=True, signed_in=False):
    env = Environment(
        loader=FileSystemLoader(ROOT / "web/templates"), autoescape=select_autoescape()
    )
    rows = [
        dict(
            kind="stock",
            product_label="Runners",
            subject="ABAT",
            company="American Battery Technology Company",
            href="/ticker/ABAT",
        ),
        dict(
            kind="memecoin",
            product_label="Memecoins",
            subject=ADDRESS,
            company=ADDRESS,
            href="/memecoins/coin/test",
        ),
        dict(
            kind="sports",
            product_label="Sports",
            subject="Biltmore Championship Asheville",
            company="PGA Tour",
            href="https://sports.test/game/golf:123",
        ),
    ]
    for row in rows:
        row.update(
            created_at="2026-09-19T12:00:00Z",
            live_label="",
            entry_label="$0.000003",
            result_label="+12.3%",
            result_tone="up",
            reward_label="+14 Flash",
            status="settled",
        )
    daily = dict(settled=2, wins=1, losses=1, avg_return_pct=1.5)
    html = env.get_template("user_calls.html").render(
        caller="caller-with-a-long-public-handle",
        calls=rows if calls else [],
        stats=dict(open=1, settled=2, wins=1, losses=1, total=3, subjects=3),
        today=dict(you=daily, machine=daily, verdict="even"),
        streak=2,
        caller_back_url="/memecoins?view=calls",
        user={"id": "test"} if signed_in else None,
        runners_origin="https://runners.test",
        sports_origin="https://sports.test",
        request=SimpleNamespace(url=SimpleNamespace(path="/u/test")),
        static_version="test",
    )
    return html


def test_caller_keeps_record_links_and_full_identifiers():
    html = caller_html()
    assert "/static/market-screen.css" in html
    assert "/static/caller-screen.css" in html
    assert "/static/mobile.css" not in html
    assert f'<span class="sr-only">{ADDRESS}</span>' in html
    assert f'title="{ADDRESS}"' in html
    assert f"{ADDRESS[:6]}…{ADDRESS[-4:]}" in html
    assert 'href="https://sports.test/game/golf:123"' in html
    assert 'href="/memecoins?view=calls"' in html
    assert html.count("<dt>Entry</dt>") == 3
    assert html.count("<dt>Result</dt>") == 3
    assert "+14 Flash" in html
    assert "2-win streak" in html
    assert "Even with Flash AI" in html


def test_empty_and_signed_in_caller():
    html = caller_html(calls=False, signed_in=True)
    assert "First Call coming soon." in html
    assert 'href="https://runners.test/calls">My Calls' in html
    assert 'caller-call"' not in html


@pytest.mark.browser
@pytest.mark.parametrize("width", [320, 390, 430, 1280])
def test_caller_rows_fit_and_keep_entry_result_visible(page, width):
    html = caller_html()
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda match: "<style>" + (ROOT / "web/static" / match[1]).read_text() + "</style>",
        html,
    )
    document = BeautifulSoup(html, "html.parser")
    for script in document.find_all("script"):
        script.decompose()
    html = str(document)
    page.set_viewport_size(dict(width=width, height=844))
    page.set_content(html)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for row in page.locator(".caller-call").all():
        subject = row.locator(".caller-subject").bounding_box()
        values = row.locator(".caller-values").bounding_box()
        assert subject and values
        assert (
            subject["y"] + subject["height"] <= values["y"]
            or subject["x"] + subject["width"] <= values["x"]
        )
        assert row.locator(".caller-values").is_visible()
        for value in row.locator(".caller-values dd").all():
            assert value.is_visible()
            assert value.evaluate("(el) => el.scrollWidth <= el.clientWidth")
