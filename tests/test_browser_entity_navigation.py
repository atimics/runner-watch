"""Entity maps retain every holding through orbit, spiral, pan and zoom."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from runner_web import main
from runner_web.cluster_worth import cluster_summary
from runner_web.entity_view import entity_view
from runner_web.market_screens import listing
from tests.test_browser_stock_map import map_payload
from tests.test_browser_ticker import _request
from tests.test_stock_map import score_current

pytestmark = pytest.mark.browser
ROOT = Path(__file__).parents[1]


def open_entity(page, width=390, count=18, reduced_motion=False):
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce" if reduced_motion else "no-preference")
    rows = [
        score_current(
            ticker=f"S{i:02}",
            price=1,
            score=(24, 58, 80)[i % 3],
            rug_score=30,
            score_components={"market": 40, "sec_event": 20},
        )
        for i in range(count)
    ]
    events = [
        {
            **map_payload()["events"][0],
            "id": f"holding-{i}",
            "ticker": row["ticker"],
            "post_shares": (i + 1) * 1000 if i else None,
            "people": [{"id": "sec:101", "name": "Example Fund", "role": "Investor"}],
        }
        for i, row in enumerate(rows)
    ]
    request = _request()
    html = main.templates.TemplateResponse(
        request,
        "stock_wallet.html",
        main.page_context(
            request,
            None,
            resolved_user=None,
            screen=listing("stocks", rows),
            wallet={"id": "sec:101", "name": "Example Fund"},
            wallet_events=events,
            wallet_cursor=None,
            wallet_ticker="S00",
            entity=entity_view(events, rows, "sec:101"),
        ),
    ).body.decode()
    html = re.sub(
        r'<link rel="stylesheet" href="/static/([^"?]+)[^"]*">',
        lambda m: "<style>" + (ROOT / "web/static" / m[1]).read_text() + "</style>",
        html,
    )
    html = re.sub(
        r'<script src="/static/([^"?]+)[^"]*"[^>]*></script>',
        lambda m: "<script>" + (ROOT / "web/static" / m[1]).read_text() + "</script>",
        html,
    )
    page.route(
        "http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html")
    )
    page.route("**/api/stocks/*/map", lambda route: route.fulfill(json={"events": []}))
    page.route(
        "**/api/stocks/*/cluster-worth",
        lambda route: route.fulfill(
            json=cluster_summary(
                route.request.url.split("/")[-2],
                [{"id": "sec:101", "name": "Example Fund"}],
                events,
                rows,
            )
        ),
    )
    page.goto("http://app.test/wallet/example")
    return page.locator("[data-entity-map]")


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_cluster_totals_switch_stock_and_show_members_and_holdings(page, width, tmp_path):
    open_entity(page, width, count=8)
    cluster = page.get_by_role("region", name="Cluster net worth", exact=True)
    expect(cluster.locator(".cluster-total")).to_have_text("$35,000")
    expect(cluster.locator(".cluster-coverage")).to_have_text(
        "1 linked entity · 8 stocks · 7 of 8 holdings valued"
    )
    expect(cluster.get_by_role("combobox", name="Stock cluster")).to_have_value("S07")
    cluster.get_by_role("combobox", name="Stock cluster").select_option("S00")
    expect(cluster.locator(".cluster-definition")).to_have_text(
        "All tracked stocks held by entities linked to S00."
    )
    expect(cluster.locator(".cluster-total")).to_have_text("$35,000")
    cluster.get_by_text("Entities in this cluster", exact=True).click()
    expect(cluster.get_by_role("link", name="Example Fund", exact=True)).to_have_attribute(
        "href", "/wallets/stocks/S00/sec%3A101"
    )
    cluster.get_by_text("Stocks in this cluster", exact=True).click()
    expect(cluster.get_by_role("link", name="S07", exact=True)).to_have_attribute(
        "href", "/stock/S07"
    )
    expect(page.get_by_role("heading", name="Holdings", exact=True)).to_be_visible()
    expect(page.locator("[data-worth-date]")).to_have_css("text-align", "right")
    assert "Filing dates" not in page.locator("[data-worth-date]").inner_text()
    expect(page.get_by_text("Legend", exact=True)).to_be_visible()
    expect(page.locator(".entity-map-tools, #entity-map-help")).to_have_count(0)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    cluster.screenshot(path=tmp_path / f"cluster-{width}.png")


def test_cluster_loading_failure_retries_and_zero_is_a_value(page):
    open_entity(page, count=1)
    page.route("**/cluster-worth", lambda route: route.fulfill(status=503))
    page.reload()
    cluster = page.get_by_role("region", name="Cluster net worth", exact=True)
    expect(cluster.get_by_text("Saved holdings are taking longer to load.")).to_be_visible()
    payload = cluster_summary(
        "S00",
        [{"id": "sec:101", "name": "<img src=x onerror=alert(1)>"}],
        [],
        [],
    )
    payload["value"] = 0
    page.route("**/cluster-worth", lambda route: route.fulfill(json=payload))
    cluster.get_by_role("button", name="Try again").click()
    expect(cluster.locator(".cluster-total")).to_have_text("$0")
    cluster.get_by_text("Entities in this cluster", exact=True).click()
    expect(cluster.get_by_role("link")).to_have_text("<img src=x onerror=alert(1)>")
    expect(cluster.locator("img")).to_have_count(0)


@pytest.mark.parametrize("width", [320, 390, 1280])
@pytest.mark.parametrize("count", [8, 18])
def test_all_holdings_orbit_in_size_order_and_keep_their_distance(page, width, count, tmp_path):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.clock.install()
    graph = open_entity(page, width, count)
    page.clock.run_for(100)
    links = graph.locator("[data-entity-stock]")
    expect(links).to_have_count(count)
    assert links.evaluate_all("nodes=>nodes.map(n=>n.dataset.entityStock)") == [
        f"S{i:02}" for i in reversed(range(count))
    ]
    expect(graph).to_have_attribute("data-layout", "ring" if count == 8 else "spiral")
    expect(page.locator("[data-entity-paging]")).to_have_count(0)
    first = links.first.get_attribute("transform")
    page.clock.run_for(1000)
    assert links.first.get_attribute("transform") != first
    distances = graph.evaluate(
        r"""graph => {
          const [cx,cy] = graph.dataset.orbitCenter.split(',').map(Number);
          const [rx,ry] = graph.dataset.orbitTrack.split(',').map(Number);
          return [...graph.querySelectorAll('[data-entity-stock]')].map(node=>{
            const [x,y] = node.dataset.orbitAnchor.split(',').map(Number);
            const [dx,dy] = node.getAttribute('transform')
              .match(/translate\(([-\d.]+) ([-\d.]+)\)/).slice(1).map(Number);
            return [Math.hypot((x-cx)/rx,(y-cy)/ry),Math.hypot((x+dx-cx)/rx,(y+dy-cy)/ry)];
          });
        }"""
    )
    assert all(after == pytest.approx(before) for before, after in distances)
    if count > 8:
        assert all(a[1] < b[1] for a, b in zip(distances, distances[1:], strict=False))
        assert float(graph.get_attribute("data-zoom")) > 1
    canvas = graph.bounding_box()
    for link in links.all()[:8]:
        box = link.bounding_box()
        assert box["x"] >= canvas["x"] - 1
        assert box["y"] >= canvas["y"] - 1
        assert box["x"] + box["width"] <= canvas["x"] + canvas["width"] + 1
        assert box["y"] + box["height"] <= canvas["y"] + canvas["height"] + 1
    graph.screenshot(path=tmp_path / f"entity-{count}-{width}.png")
    graph.focus()
    graph.press("Home")
    home = graph.get_attribute("viewBox")
    graph.focus()
    graph.press("+")
    assert float(graph.get_attribute("data-zoom")) > 1
    graph.focus()
    graph.press("ArrowRight")
    assert graph.get_attribute("viewBox") != home
    graph.press("Home")
    assert graph.get_attribute("viewBox") == home
    expect(graph).to_have_attribute("data-zoom", "1")
    expect(links).to_have_count(count)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert errors == []
    graph.screenshot(path=tmp_path / f"entity-fit-{count}-{width}.png")


def test_reduced_motion_keeps_all_stocks_and_zoom_available(page):
    graph = open_entity(page, count=18, reduced_motion=True)
    expect(graph.locator("[data-entity-stock]")).to_have_count(18)
    expect(graph.locator("[data-entity-stock]").first).not_to_have_attribute(
        "transform", re.compile("translate")
    )
    graph.focus()
    graph.press("+")
    assert float(graph.get_attribute("data-zoom")) > 1
    graph.focus()
    graph.press("Home")
    expect(graph).to_have_attribute("data-zoom", "1")


def test_touch_pinch_drag_and_stock_tap(browser):
    context = browser.new_context(viewport={"width": 390, "height": 900}, has_touch=True)
    page = context.new_page()
    try:
        page.clock.install()
        graph = open_entity(page, count=18)
        graph.scroll_into_view_if_needed()
        box = graph.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        client = context.new_cdp_session(page)
        client.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchStart",
                "touchPoints": [{"x": x - 25, "y": y, "id": 1}, {"x": x + 25, "y": y, "id": 2}],
            },
        )
        client.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchMove",
                "touchPoints": [{"x": x - 60, "y": y, "id": 1}, {"x": x + 60, "y": y, "id": 2}],
            },
        )
        client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        assert float(graph.get_attribute("data-zoom")) > 1.5
        graph.focus()
        graph.press("Home")
        page.clock.run_for(32)
        start = graph.locator('[data-entity-stock="S17"] .entity-glyph-backplate').bounding_box()
        x, y = start["x"] + start["width"] / 2, start["y"] + start["height"] / 2
        before = graph.get_attribute("viewBox")
        client.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchStart",
                "touchPoints": [{"x": x, "y": y, "id": 1}],
            },
        )
        client.send(
            "Input.dispatchTouchEvent",
            {
                "type": "touchMove",
                "touchPoints": [{"x": x + 40, "y": y + 30, "id": 1}],
            },
        )
        client.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        assert graph.get_attribute("viewBox") != before
        expect(page).to_have_url("http://app.test/wallet/example")
        expect(graph).not_to_have_class(re.compile("orbit-interacting"))
        graph.focus()
        graph.press("Home")
        page.clock.run_for(32)
        # Tap the current position of the orbiting stock, as a finger would.
        target = graph.locator('[data-entity-stock="S17"] .entity-glyph-backplate').bounding_box()
        tap = {"x": target["x"] + target["width"] / 2, "y": target["y"] + target["height"] / 2}
        assert (
            page.evaluate(
                "p=>document.elementFromPoint(p.x,p.y)?.closest('[data-entity-stock]')?.dataset.entityStock",
                tap,
            )
            == "S17"
        )
        assert page.evaluate(
            "p=>document.elementFromPoint(p.x,p.y)?.classList.contains('entity-glyph-backplate')",
            tap,
        )
        page.evaluate("""() => {
          window.touchTrace = [];
          for (const type of ['pointerdown','pointerup','pointercancel','click']) {
            document.addEventListener(type, e => window.touchTrace.push({
              type, target:e.target.tagName,
              stock:e.target.closest('[data-entity-stock]')?.dataset.entityStock,
              x:e.clientX, y:e.clientY, time:performance.now(),
            }), true);
          }
        }""")
        page.touchscreen.tap(tap["x"], tap["y"])
        try:
            expect(page).to_have_url("http://app.test/stock/S17")
        except AssertionError:
            pytest.fail(f"Stock tap did not navigate: {page.evaluate('window.touchTrace')}")
    finally:
        context.close()
