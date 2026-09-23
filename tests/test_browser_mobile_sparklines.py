"""Real mobile list rendering, independent chart/state colors and refresh parity."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from runner_web.market_screens import listing
from tests.test_market_screens import render
from tests.test_stock_indicator import stock

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser


def points(*prices):
    return [
        {"time": f"2026-09-22T14:{i * 5:02d}:00Z", "price": price} for i, price in enumerate(prices)
    ]


def examples():
    return [
        stock(
            ticker="NOVA",
            price=10.8,
            change_pct=8,
            score_components={"market": 17, "sec_event": 4, "news": 1, "social_search": 2},
            company="North Valley",
            score=24,
            stage="WATCH",
            trade_state="WATCH",
            evidence_gate={"state": "gathering"},
        ),
        stock(
            ticker="MESA",
            company="Mesa Computing",
            score=58,
            price=11.4,
            change_pct=14,
            score_components={"market": 40.4, "sec_event": 9.6, "news": 3, "social_search": 5},
        ),
        stock(
            ticker="ORBIT",
            price=12,
            change_pct=20,
            score_components={"market": 60, "sec_event": 10, "news": 5, "social_search": 5},
            company="Orbit Industries",
            stage="EXTENDED",
            score=80,
            rug_score=35,
            rug_level="GUARDED",
            evidence_gate={"state": "gathering"},
        ),
        stock(
            ticker="RIVER",
            price=8.5,
            change_pct=-15,
            sentiment="risk",
            score_components={"market": 32, "sec_event": 8, "news": 3, "social_search": 2},
            company="River Systems",
            stage="WATCH",
            trade_state="AVOID",
            score=45,
            rug_score=70,
            rug_level="HIGH",
            eligibility={"state": "blocked"},
        ),
        stock(
            ticker="SETUP",
            price=10.4,
            change_pct=4,
            score_components={"market": 40, "sec_event": 6, "news": 3, "social_search": 2},
            company="Early Systems",
            stage="EARLY",
            trade_state="ARMED",
            score=51,
            evidence_gate={"state": "gathering"},
        ),
        stock(
            ticker="PAUSE",
            score_components={},
            company="Pending Industries",
            score=None,
            rug_score=None,
            sentiment="gap",
            eligibility={"state": "unknown"},
            evidence_gate=None,
        ),
    ]


def chart_examples():
    return {
        "charts": {
            "NOVA": points(10, 10.1, 10.08, 10.18, 10.3, 10.5, 10.4, 10.8),
            "MESA": points(10, 10.3, 10.25, 10.7, 10.6, 11, 11.1, 11.4),
            "ORBIT": points(10, 10.5, 10.3, 10.7, 11.2, 11.1, 11.6, 12),
            "RIVER": points(10, 9.7, 9.8, 9.4, 9.1, 9.2, 8.8, 8.5),
            "SETUP": points(10, 10, 10.1, 10, 10.2, 10.3, 10.2, 10.4),
            "PAUSE": [],
        }
    }


def open_list(page, rows=None, payload=None, width=390, page_status=None):
    state = {
        "rows": examples() if rows is None else rows,
        "payload": chart_examples() if payload is None else payload,
        "status": 200,
        "chart_requests": 0,
        "page_requests": 0,
        "page_status": dict(page_status or {}),
    }
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == "/":
            state["page_requests"] += 1
            route.fulfill(content_type="text/html", body=render(listing("stocks", state["rows"])))
        elif path == "/api/pulse/charts":
            state["chart_requests"] += 1
            payload = state["payload"]
            offset = 0
            if "pages" in payload:
                offset = int(parse_qs(urlsplit(route.request.url).query).get("offset", ["0"])[0])
                payload = payload["pages"].get(offset, {"charts": {}})
            route.fulfill(status=state["page_status"].get(offset, state["status"]), json=payload)
        elif path.startswith("/static/"):
            asset = ROOT / "web/static" / Path(path).name
            content_type = "text/css" if asset.suffix == ".css" else "application/javascript"
            route.fulfill(content_type=content_type, body=asset.read_bytes())
        else:
            route.fulfill(status=404, body="Not found")

    page.route("http://127.0.0.1/**", serve)
    page.set_viewport_size({"width": width, "height": 844})
    page.goto("http://127.0.0.1/")
    expect(page.locator(".mini-chart").first).not_to_have_attribute("data-history", "pending")
    state["errors"] = errors
    return state


@pytest.mark.parametrize("width", [320, 390, 760])
def test_mobile_replaces_chip_and_preserves_exact_terms_palette_and_checks(page, width):
    state = open_list(page, width=width)
    terms = ["WATCH", "RUNNING", "EXTENDED", "AVOID", "SETUP", "PAUSED"]
    palette = [
        "rgb(196, 167, 239)",
        "rgb(165, 229, 185)",
        "rgb(255, 173, 112)",
        "rgb(239, 153, 164)",
        "rgb(115, 206, 255)",
        "rgb(150, 164, 155)",
    ]
    assert page.locator(".ticker-run-status").all_text_contents() == [
        "WATCH",
        "RUNNING",
        "▲EXTENDED",
        "▲AVOID",
        "SETUP",
        "PAUSED",
    ]
    for i, row in enumerate(page.locator(".ticker").all()):
        expect(row.locator(":scope>.tag")).to_be_hidden()
        status = row.locator(".ticker-run-status")
        expect(status).to_be_visible()
        expect(status).to_contain_text(terms[i])
        assert status.evaluate("el => getComputedStyle(el).color") == palette[i]
        trend = row.locator(".ticker-trend").bounding_box()
        chart = row.locator(".mini-chart").bounding_box()
        label = status.bounding_box()
        assert chart["y"] + chart["height"] <= label["y"]
        bounds = [trend] + [
            row.locator(s).bounding_box()
            for s in (".ticker-name", ".ticker-value", ".ticker-score")
        ]
        assert all(
            a["x"] + a["width"] <= b["x"] + 1 for a, b in zip(bounds, bounds[1:], strict=False)
        )
        assert row.bounding_box()["height"] <= 64
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    expect(page.locator(".indicator-glyph")).to_have_count(6)
    expect(page.locator(".ticker-verified")).to_have_count(1)
    expect(page.get_by_role("button", name=re.compile("alert|bell", re.I))).to_have_count(0)
    assert state["chart_requests"] == 1  # one batch, not six requests
    assert state["errors"] == []


def test_chart_direction_does_not_recolor_or_rename_watch_setup_extended(page):
    rows = [
        stock(ticker="UP", stage="WATCH", trade_state="WATCH"),
        stock(ticker="DOWN", stage="EARLY", trade_state="ARMED"),
        stock(ticker="FLAT", stage="EXTENDED"),
    ]
    state = open_list(
        page,
        rows,
        {"charts": {"UP": points(10, 11), "DOWN": points(10, 9), "FLAT": points(10, 10)}},
    )
    for ticker, tone in [("UP", "rising"), ("DOWN", "falling"), ("FLAT", "flat")]:
        expect(page.locator(f'.mini-chart[data-ticker="{ticker}"]')).to_have_class(re.compile(tone))
    expect(page.locator(".status-watch")).to_have_text("WATCH")
    expect(page.locator(".status-setup")).to_have_text("SETUP")
    expect(page.locator(".status-extended")).to_have_text("EXTENDED")
    assert state["errors"] == []


@pytest.mark.parametrize("width", [761, 1280])
def test_desktop_retains_chip_and_existing_chart_column(page, width):
    open_list(page, width=width)
    for row in page.locator(".ticker").all():
        expect(row.locator(":scope>.tag")).to_be_visible()
        expect(row.locator(".ticker-run-status")).to_be_hidden()
        chart = row.locator(".mini-chart").bounding_box()
        quote = row.locator(".ticker-value").bounding_box()
        assert chart["x"] >= quote["x"] + quote["width"]
        assert chart["width"] == 64 and chart["height"] == 18
    assert (
        page.locator(".tag-running").first.evaluate("el => getComputedStyle(el).backgroundColor")
        == "rgb(165, 229, 185)"
    )


@pytest.mark.parametrize(
    "series,kind",
    [
        ([], "unavailable"),
        (None, "unavailable"),
        (
            [
                {"time": "invalid", "price": 12},
                {"time": "2026-09-22T14:00:00Z", "price": True},
                {"time": "2026-09-22T14:05:00Z", "price": 0},
                {"time": "2026-09-22T14:10:00Z", "price": -3},
            ],
            "unavailable",
        ),
        (points(12), "single"),
        (points(12, 12), "flat"),
    ],
)
def test_missing_sparse_and_flat_histories_never_get_fabricated_paths(page, series, kind):
    state = open_list(page, [stock(ticker="TEST")], {"charts": {"TEST": series}})
    chart = page.locator(".mini-chart")
    expect(chart.locator(".chart-placeholder")).to_have_count(0)
    if kind == "unavailable":
        expect(chart).to_have_attribute("data-history", "unavailable")
        expect(chart.locator("path, circle")).to_have_count(0)
        expect(chart).to_have_attribute("aria-label", "TEST: price history unavailable")
    elif kind == "single":
        expect(chart.locator("path")).to_have_count(0)
        expect(chart.locator(".mini-chart-point")).to_have_count(1)
        expect(chart).to_have_attribute("aria-label", re.compile("one saved price"))
    else:
        expect(chart.locator(".mini-chart-line")).to_have_attribute("d", "M1.00 9.00 L63.00 9.00")
    assert state["errors"] == []


def test_sparkline_orders_times_deduplicates_and_keeps_real_time_spacing(page):
    payload = {
        "charts": {
            "TEST": [
                {"time": "2026-09-22T15:00:00Z", "price": 110},
                {"time": "2026-09-22T14:00:00Z", "price": 99},
                {"time": "2026-09-22T14:10:00Z", "price": "103"},
                {"time": "2026-09-22T14:00:00Z", "price": 100},
            ]
        },
        "annotations": {"TEST": [{"type": "pulse_entry", "time": "2026-09-22T16:00:00Z"}]},
    }
    open_list(page, [stock(ticker="TEST")], payload)
    expect(page.locator(".mini-chart-line")).to_have_attribute(
        "d", "M1.00 9.00 L11.33 6.90 L63.00 2.00"
    )
    expect(page.locator(".pulse-entry-dot")).to_have_count(0)  # future marker is not a saved point
    expect(page.locator(".mini-chart")).to_have_attribute("aria-label", re.compile("\\+10.0%"))


def test_list_refresh_updates_paths_and_status_without_losing_filter_or_glyph(page):
    page.clock.install()
    state = open_list(page)
    page.get_by_role("button", name=re.compile("1 running")).click()
    expect(page.locator(".ticker:visible")).to_have_count(1)
    initial = page.locator('.mini-chart[data-ticker="MESA"] .mini-chart-line').get_attribute("d")
    state["rows"][1]["evidence_gate"] = {"state": "gathering"}
    state["payload"]["charts"]["MESA"] = points(10, 9.8, 9.5)
    page.clock.fast_forward(61000)
    chart = page.locator('.mini-chart[data-ticker="MESA"]')
    expect(chart).to_have_class(re.compile("falling"))
    assert chart.locator(".mini-chart-line").get_attribute("d") != initial
    expect(page.locator(".ticker:visible")).to_have_count(1)
    expect(page.locator(".ticker:visible .ticker-run-status")).to_have_text("RUNNING")
    expect(page.locator(".ticker-verified")).to_have_count(0)
    expect(page.locator(".indicator-glyph")).to_have_count(6)
    assert state["chart_requests"] == 2
    state["rows"][1].update(stage="EARLY", trade_state="ARMED")
    page.clock.fast_forward(61000)
    expect(page.locator("[data-filter-empty]")).to_be_visible()
    expect(page.locator('.ticker[href="/stock/MESA"] .ticker-run-status')).to_have_text("SETUP")
    assert state["errors"] == []


def test_failed_refresh_keeps_saved_history_and_recovers_without_stale_cache(page):
    page.clock.install()
    state = open_list(page, [stock(ticker="TEST")], {"charts": {"TEST": points(10, 11)}})
    path = page.locator(".mini-chart-line").get_attribute("d")
    state["status"] = 503
    page.clock.fast_forward(61000)
    expect(page.locator(".mini-chart")).to_have_attribute("data-history", "stale")
    expect(page.locator(".mini-chart-line")).to_have_attribute("d", path)
    expect(page.locator(".mini-chart")).to_have_attribute(
        "aria-label", re.compile("Refresh unavailable")
    )
    state.update(status=200, payload={"charts": {}})
    page.clock.fast_forward(61000)
    expect(page.locator(".mini-chart")).to_have_attribute("data-history", "unavailable")
    expect(page.locator(".mini-chart-line")).to_have_count(0)
    state["payload"] = {"charts": {"TEST": points(10, 9)}}
    page.clock.fast_forward(61000)
    expect(page.locator(".mini-chart")).to_have_class(re.compile("falling"))
    expect(page.locator(".mini-chart")).to_have_attribute("data-history", "available")
    assert state["errors"] == []


def test_new_row_receives_history_without_unlisted_outlier_distorting_scale(page):
    page.clock.install()
    state = open_list(
        page,
        [stock(ticker="TEST")],
        {"charts": {"TEST": points(100, 101), "OUTLIER": points(10, 20)}},
    )
    old = page.locator(".mini-chart-line").get_attribute("d")
    state["rows"].append(stock(ticker="NEW"))
    state["payload"] = {"charts": {"TEST": points(100, 101), "NEW": points(100, 99)}}
    page.clock.fast_forward(61000)
    expect(page.locator(".mini-chart.loaded")).to_have_count(2)
    expect(page.locator('.mini-chart[data-ticker="NEW"]')).to_have_class(re.compile("falling"))
    assert (
        page.locator('.mini-chart[data-ticker="TEST"] .mini-chart-line').get_attribute("d") == old
    )
    assert state["chart_requests"] == 2


def test_concurrent_chart_refreshes_share_one_request(page):
    state = open_list(page)
    page.evaluate(
        "Promise.all(Array.from({length:8}, () => TickerRow.loadCharts('/api/pulse/charts')))"
    )
    assert state["chart_requests"] == 2


def test_charts_load_for_rows_after_the_first_fifty(page):
    rows = [stock(ticker=f"STK{i}") for i in range(53)]
    payload = {
        "pages": {
            0: {
                "charts": {f"STK{i}": points(10, 11) for i in range(50)},
                "next_offset": 50,
                "has_more": True,
            },
            50: {
                "charts": {f"STK{i}": points(10, 11) for i in range(50, 53)},
                "next_offset": 53,
                "has_more": False,
            },
        }
    }
    state = open_list(page, rows, payload)
    assert state["chart_requests"] == 1
    expect(page.locator('.mini-chart[data-ticker="STK52"]')).to_have_attribute(
        "data-history", "pending"
    )
    page.locator('.mini-chart[data-ticker="STK52"]').scroll_into_view_if_needed()
    expect(page.locator('.mini-chart[data-history="available"]')).to_have_count(53)
    expect(page.locator('.mini-chart[data-ticker="STK52"]')).to_have_attribute(
        "data-history", "available"
    )
    assert state["chart_requests"] == 2


def test_later_chart_page_failure_keeps_earlier_charts_and_retries(page):
    page.clock.install()
    rows = [stock(ticker=f"STK{i}") for i in range(53)]
    payload = {
        "pages": {
            0: {
                "charts": {f"STK{i}": points(10, 11) for i in range(50)},
                "next_offset": 50,
                "has_more": True,
            },
            50: {
                "charts": {f"STK{i}": points(10, 11) for i in range(50, 53)},
                "next_offset": 53,
                "has_more": False,
            },
        }
    }
    state = open_list(page, rows, payload, page_status={50: 503})
    page.locator('.mini-chart[data-ticker="STK52"]').scroll_into_view_if_needed()
    expect(page.locator('.mini-chart[data-history="available"]')).to_have_count(50)
    expect(page.locator('.mini-chart[data-history="unavailable"]')).to_have_count(3)
    state["page_status"][50] = 200
    page.clock.fast_forward(5100)
    expect(page.locator('.mini-chart[data-history="available"]')).to_have_count(53)
    state["page_status"][50] = 503
    page.evaluate("TickerRow.loadCharts('/api/pulse/charts', 50, true)")
    expect(page.locator('.mini-chart[data-history="available"]')).to_have_count(50)
    expect(page.locator('.mini-chart[data-history="stale"]')).to_have_count(3)
    assert state["errors"] == []


def test_initial_fetch_failure_never_draws_fake_price_and_retries(page):
    page.clock.install()
    state = open_list(page, [stock(ticker="TEST")], {"unexpected": "response"})
    expect(page.locator(".mini-chart")).to_have_attribute("data-history", "unavailable")
    state["payload"] = {"charts": {"TEST": points(12, 13)}}
    page.clock.fast_forward(61000)
    expect(page.locator(".mini-chart")).to_have_attribute("data-history", "available")
    assert state["errors"] == []
