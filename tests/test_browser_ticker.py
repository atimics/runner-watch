from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, Route
from starlette.requests import Request

from runner_web import main as web_main

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.browser


def _request() -> Request:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/t/TEST",
            "headers": [(b"host", b"runners.rati.chat")],
            "scheme": "https",
            "server": ("runners.rati.chat", 443),
            "client": ("127.0.0.1", 1234),
            "query_string": b"",
        }
    )
    request.state.csp_nonce = "browser-test"
    return request


def _rendered_ticker(
    *,
    signed_in: bool = False,
    comment_generation_enabled: bool = True,
    inline_script: bool = False,
    current_overrides: dict[str, Any] | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> str:
    thesis = web_main._ranker_directional_thesis(
        {
            "probability_down": 0.15,
            "probability_timeout": 0.31,
            "probability_up": 0.54,
            "expected_return_pct": 3.0,
            "model_id": "ranker-browser-test",
            "model_status": "shadow",
            "created_at": "2026-08-28T18:00:00+00:00",
        }
    )
    detail = {
        "ticker": "TEST",
        "company": "Test Systems",
        "exchange": "NASDAQ",
        "coin_label": "TE",
        "coin_tone": 3,
        "directional_thesis": thesis,
        "current": {
            "price": 2.15,
            "change_pct": 8.4,
            "event_at": "2026-08-28T18:00:00+00:00",
            "source": "market",
            "score": 67,
            "setup_score": 67,
            "rug_score": 42,
            "rug_level": "guarded",
            "trade_state": "WATCH",
            "drawdown_52w_pct": 31,
            "crash_candidate": False,
            "state_reason": "The setup is active, but risk remains separate.",
            "relative_volume": 4.8,
            "momentum_15m_pct": 2.1,
            "recent_dollar_volume": 1_500_000,
            "signals": ["Fresh volume"],
            "risks": ["Wide spread"],
            "issuer_risk": {},
            "return_1h_pct": None,
            "return_1d_pct": None,
            "return_5d_pct": None,
        },
        "events": [],
        "trade_pressure": {},
        "evidence_gate": {
            "state": "near",
            "summary": "More evidence needed",
            "count": 3,
            "threshold": 4,
            "blockers": ["Wait for one more source"],
            "checks": [],
            "baseline_summary": "Building the same-time baseline.",
        },
    }
    detail["current"].update(current_overrides or {})
    signed_in_context = {}
    if signed_in:
        signed_in_context = {
            "user": {"id": "browser-user"},
            "comment_avatar": web_main.comment_avatar_profile(
                "Quiet Signal", "browser-seed", "filing_sleuth"
            ),
            "flash_wallet": {
                "balance": 150,
                "can_claim": False,
                "claim_day": "2026-08-28",
                "report_cost": 100,
            },
            "caller_summary": {
                "average_return_pct": None,
                "wins": 0,
                "losses": 0,
            },
            "comment_generation_enabled": comment_generation_enabled,
        }
    request = _request()
    response = web_main.templates.TemplateResponse(
        request=request,
        name="ticker.html",
        context=web_main.page_context(
            request,
            None,
            detail=detail,
            comments=comments or [],
            comment_count=len(comments or []),
            active_call=None,
            calls=[],
            latest_commission=None,
            flash_report={
                "state": "unavailable",
                "label": "Report unavailable",
                "detail": "Try again later",
                "enabled": False,
                "href": None,
                "job_id": None,
                "message": "",
                "status_tone": "",
                "start_url": "/api/research/TEST",
            },
            active_tab="pulse",
            **signed_in_context,
        ),
    )
    html = response.body.decode()
    styles = "\n".join(
        (ROOT / "web/static" / name).read_text()
        for name in (
            "mobile.css",
            "product-system.css",
        )
    )
    html = html.replace("<head>", '<head><base href="http://app.test/">')
    html = html.replace("</head>", f"<style>{styles}</style></head>")
    if inline_script:
        for script_name in ("content-notices.js", "flash-comments.js", "ticker-detail.js"):
            script = (ROOT / "web/static" / script_name).read_text()
            html = re.sub(
                rf'<script src="/static/{re.escape(script_name)}[^\"]*"[^>]*></script>',
                lambda _match, source=script: f"<script>{source}</script>",
                html,
            )
    return re.sub(r'<link rel="stylesheet"[^>]*>', "", html)


def test_model_path_receipt_stays_compact_and_keeps_risk_separate(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_content(_rendered_ticker(), wait_until="domcontentloaded")

    card = page.locator(".model-path-card")
    risk = page.locator(".risk-decision")
    assert card.is_visible()
    assert "Model estimates" in card.inner_text()
    assert "Actual outcomes can differ" in card.inner_text()
    assert [
        " ".join(text.split())
        for text in card.locator(".model-path-outcomes > div").all_inner_texts()
    ] == ["15% −4% first", "31% No barrier", "54% +8% first"]
    assert card.bounding_box()["height"] < 170
    assert risk.bounding_box()["y"] > card.bounding_box()["y"] + card.bounding_box()["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True


@pytest.mark.parametrize(
    ("values", "display"),
    [
        ((None, None, None), ["unknown", "unknown", "unknown"]),
        ((0, 0, 0), ["0", "0", "0%"]),
        ((67, 42, 31), ["67", "42", "31%"]),
    ],
)
def test_ticker_risk_readings_keep_missing_values_and_zero_distinct(
    page: Page, values: tuple[float | None, ...], display: list[str]
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_content(
        _rendered_ticker(
            current_overrides=dict(
                zip(("setup_score", "rug_score", "drawdown_52w_pct"), values, strict=True)
            )
        ),
        wait_until="domcontentloaded",
    )

    assert page.locator(".risk-grid b").all_inner_texts() == display
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True


@pytest.mark.parametrize(
    ("ai_generated", "model"),
    [(True, "test/model-<script>alert(1)</script>"), (True, ""), (False, "test/model")],
)
def test_saved_comments_show_generated_authorship_and_model(
    page: Page, ai_generated: bool, model: str
) -> None:
    comment = {
        "id": "saved-comment",
        "avatar": web_main.comment_avatar_profile("Tape Reader", "browser-seed", "filing_sleuth"),
        "author_label": "AI avatar" if ai_generated else "Account avatar",
        "ai_generated": ai_generated,
        "generation_model": model,
        "is_owner": False,
        "created_at": "2026-08-28T18:00:00+00:00",
        "body": "Volume is improving.",
    }
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_content(_rendered_ticker(comments=[comment]), wait_until="domcontentloaded")

    rendered = page.locator('[data-comment-id="saved-comment"]')
    assert rendered.locator(".comment-owner").inner_text() == comment["author_label"]
    if ai_generated:
        assert rendered.locator(".comment-model").inner_text() == f"Model {model or 'unknown'}"
        assert rendered.locator(".comment-model").is_visible()
    else:
        assert rendered.locator(".comment-model").count() == 0
    assert rendered.locator("script").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True


def test_flash_comment_action_is_one_compact_row_without_explainer_copy(
    page: Page,
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_content(_rendered_ticker(signed_in=True), wait_until="domcontentloaded")

    discussion = page.locator(".discussion-section")
    action = discussion.locator(".ai-comment-action")
    assert action.is_visible()
    assert action.locator("p").count() == 0
    assert action.locator("#commentStatus").text_content() == "10 Flash"
    assert action.locator("#generateComment").text_content() == "Post with avatar"
    assert action.bounding_box()["height"] <= 46
    copy = discussion.inner_text()
    assert "Persistent avatars" not in copy
    assert "ability guides" not in copy
    assert "Start the read" not in copy

    page.set_content(
        _rendered_ticker(signed_in=True, comment_generation_enabled=False),
        wait_until="domcontentloaded",
    )
    assert page.locator("#generateComment").is_disabled()
    assert page.locator("#commentStatus").text_content() == "Unavailable"


def test_failed_flash_report_restores_balance_without_internal_error_or_layout_jump(
    page: Page,
) -> None:
    action = {
        "state": "available",
        "label": "Generate report",
        "detail": "100 Flash · private 1h",
        "enabled": True,
        "href": None,
        "job_id": None,
        "message": "",
        "status_tone": "",
        "start_url": "/api/research/TEST",
    }
    control = str(
        web_main.templates.env.get_template("_flash_report_action.html").module.flash_report_action(
            action
        )
    )
    styles = (ROOT / "web/static/mobile.css").read_text()
    script = (ROOT / "web/static/flash-report.js").read_text()
    html = f"""
    <!doctype html><html><head><base href="http://app.test/"><style>
    :root{{--green:#57e389}} body{{margin:20px;background:#090b0b;color:#edf2ef}}
    .flash-shell{{width:210px}} {styles}
    </style></head><body>
    <b data-flash-balance>100</b><div class="flash-shell">{control}</div>
    <script>{script}</script></body></html>
    """

    def start_report(route: Route) -> None:
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps({"status": "running", "job_id": "job-one", "balance": 0}),
        )

    def failed_report(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "status": "failed",
                    "retryable": True,
                    "balance": 100,
                    "error": "OpenRouter returned a report without a usable thesis.",
                }
            ),
        )

    page.route("**/api/research/TEST", start_report)
    page.route("**/api/research/jobs/job-one", failed_report)
    page.set_content(html, wait_until="domcontentloaded")
    initial_height = page.locator(".flash-shell").bounding_box()["height"]

    page.locator("#commissionButton").click()
    page.locator("#commissionStatus").wait_for(state="visible")
    page.wait_for_function(
        "document.querySelector('#commissionStatus').textContent.includes('No Flash was charged')"
    )

    assert page.locator("[data-flash-balance]").text_content() == "100"
    assert page.locator("#commissionButton strong").text_content() == "Try again"
    assert page.locator("#commissionStatus").text_content() == (
        "Report couldn't be generated. No Flash was charged."
    )
    assert "OpenRouter" not in page.locator("body").inner_text()
    assert page.locator(".flash-shell").bounding_box()["height"] == initial_height


@pytest.mark.parametrize("model", ["test/model-<script>alert(1)</script>", ""])
def test_comment_recovers_from_proxy_timeout_without_another_click(page: Page, model: str) -> None:
    request_keys: list[str] = []

    def comment_response(route: Route) -> None:
        request_keys.append(route.request.headers["idempotency-key"])
        if len(request_keys) == 1:
            route.fulfill(status=502, content_type="text/html", body="Gateway timeout")
        elif len(request_keys) == 2:
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": "Still drafting", "retryable": True}),
            )
        else:
            route.fulfill(
                status=201,
                content_type="application/json",
                body=json.dumps(
                    {
                        "comment": {
                            "id": "comment-1",
                            "avatar": {
                                "name": "Tape Reader",
                                "ability": "Liquidity Reader",
                                "tone": 0,
                                "frame": 0,
                                "eyes": 0,
                                "signal": 0,
                            },
                            "created_at": "2026-08-28T18:00:00+00:00",
                            "body": "Volume is improving, but the spread is still the risk.",
                            "ai_generated": True,
                            "generation_model": model,
                            "author_label": "AI avatar",
                            "is_owner": True,
                        },
                        "count": 1,
                        "balance": 90,
                    }
                ),
            )

    page.route("**/api/comments/stock/TEST", comment_response)
    page.route(
        "**/api/t/TEST/chart",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"points":[],"annotations":[]}',
        ),
    )
    page.route(
        "**/api/t/TEST/pressure",
        lambda route: route.fulfill(status=404, content_type="application/json", body="{}"),
    )
    page.set_content(
        _rendered_ticker(signed_in=True, inline_script=True),
        wait_until="domcontentloaded",
    )

    page.locator("#generateComment").click()
    page.wait_for_function(
        "document.querySelector('#commentStatus').textContent.startsWith('Posted')"
    )

    assert len(request_keys) == 3
    assert len(set(request_keys)) == 1
    assert page.locator("#commentList > li").count() == 1
    assert page.locator("#commentList .comment-owner").first.inner_text() == "AI avatar"
    assert page.locator("#commentList .comment-model").inner_text() == f"Model {model or 'unknown'}"
    assert page.locator("#commentList script").count() == 0
    assert page.locator("#commentStatus").text_content() == "Posted"
    assert page.locator("#discussionCount").text_content() == "1"


def _comment_with_notices() -> dict[str, Any]:
    return {
        "id": "comment-disclosed",
        "avatar": web_main.comment_avatar_profile("Tape Reader", "browser-seed", "filing_sleuth"),
        "author_label": "AI avatar",
        "ai_generated": True,
        "generation_model": "test/model",
        "is_owner": True,
        "created_at": "2026-09-05T18:00:00+00:00",
        "body": "Volume is improving.",
        "disclosures": [
            {
                "id": "notice-holdings",
                "kind": "holdings",
                "label": "Holdings",
                "text": "I hold shares. <script>alert(1)</script>",
                "reason": None,
                "created_at": "2026-09-05T18:00:00+00:00",
                "recorded_by": "author",
            }
        ],
        "corrections": [
            {
                "id": "notice-correction",
                "kind": "correction",
                "label": "Correction",
                "text": "The source volume was revised to 2 million shares.",
                "reason": "The source corrected its volume field.",
                "created_at": "2026-09-05T18:30:00+00:00",
                "recorded_by": "operator",
            }
        ],
    }


def test_saved_comment_keeps_public_disclosure_and_correction_visible(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_content(
        _rendered_ticker(comments=[_comment_with_notices()]), wait_until="domcontentloaded"
    )
    notices = page.locator("[data-comment-id='comment-disclosed'] .content-notices")
    assert notices.is_visible()
    assert "Disclosure" in notices.inner_text()
    assert "Holdings" in notices.inner_text()
    assert "Correction" in notices.inner_text()
    assert "The source corrected its volume field." in notices.inner_text()
    assert "I hold shares. <script>alert(1)</script>" in notices.inner_text()
    assert notices.locator("script").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def _open_disclosure_composer(page: Page) -> None:
    page.add_init_script(
        f"localStorage.setItem('rati-release:rati-runners-{web_main.APP_VERSION}', '1')"
    )
    html = _rendered_ticker(signed_in=True, inline_script=True)
    page.route(
        "http://app.test/**", lambda route: route.fulfill(body=html, content_type="text/html")
    )
    page.route("**/api/t/TEST/**", lambda route: route.fulfill(json={"points": []}))
    page.goto("http://app.test/t/TEST", wait_until="domcontentloaded")
    page.get_by_text("Add a public disclosure", exact=True).click()


def test_comment_retry_preserves_disclosure_and_renders_returned_notices(page: Page) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []
    _open_disclosure_composer(page)

    def post_comment(route: Route) -> None:
        requests.append(
            (route.request.headers["idempotency-key"], json.loads(route.request.post_data or "{}"))
        )
        if len(requests) == 1:
            route.fulfill(status=502, content_type="text/html", body="Gateway timeout")
        else:
            route.fulfill(
                status=201,
                json={
                    "comment": _comment_with_notices(),
                    "count": 1,
                    "balance": 90,
                },
            )

    page.route("**/api/comments/stock/TEST", post_comment)
    page.get_by_role("combobox", name="Disclosure relationship").select_option("holdings")
    page.get_by_role("textbox", name="Public details").fill("I hold shares.")
    page.get_by_role("button", name="Post with avatar").click()
    page.wait_for_function("document.querySelector('#commentStatus').textContent === 'Posted'")
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0][1] == {"disclosure_kind": "holdings", "disclosure": "I hold shares."}
    notices = page.locator("#commentList .content-notices")
    assert "Disclosure · Holdings" in notices.inner_text()
    assert "Correction" in notices.inner_text()
    assert "I hold shares. <script>alert(1)</script>" in notices.inner_text()
    assert notices.locator("script").count() == 0
    assert page.get_by_role("textbox", name="Public details").input_value() == ""
    assert page.get_by_role("combobox", name="Disclosure relationship").is_enabled()


def test_comment_disclosure_requires_a_relationship_and_public_details(page: Page) -> None:
    _open_disclosure_composer(page)
    requests: list[str] = []
    page.route("**/api/comments/stock/TEST", lambda route: requests.append(route.request.url))
    page.get_by_role("combobox", name="Disclosure relationship").select_option("sponsorship")
    page.get_by_role("button", name="Post with avatar").click()
    assert page.locator("#commentStatus").inner_text() == (
        "Choose a relationship and add at least 3 characters of public details."
    )
    assert page.get_by_role("textbox", name="Public details").evaluate(
        "node => node === document.activeElement"
    )
    assert requests == []


def test_pending_comment_restores_its_disclosure_after_reload(page: Page) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []
    _open_disclosure_composer(page)

    def post_comment(route: Route) -> None:
        requests.append(
            (route.request.headers["idempotency-key"], json.loads(route.request.post_data or "{}"))
        )
        if len(requests) > 1:
            route.fulfill(
                status=201,
                json={
                    "comment": _comment_with_notices(),
                    "count": 1,
                    "balance": 90,
                },
            )

    page.route("**/api/comments/stock/TEST", post_comment)
    page.get_by_role("combobox", name="Disclosure relationship").select_option("holdings")
    page.get_by_role("textbox", name="Public details").fill("I hold shares.")
    with page.expect_request("**/api/comments/stock/TEST"):
        page.get_by_role("button", name="Post with avatar").click()
    page.reload(wait_until="domcontentloaded")
    page.get_by_text("Add a public disclosure", exact=True).click()
    assert page.get_by_role("textbox", name="Public details").input_value() == "I hold shares."
    assert page.get_by_role("textbox", name="Public details").is_disabled()
    page.get_by_role("button", name="Post with avatar").click()
    page.wait_for_function("document.querySelector('#commentStatus').textContent === 'Posted'")
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert page.locator("#commentList > li").count() == 1
