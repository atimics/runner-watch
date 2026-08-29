from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page
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


def _rendered_ticker() -> str:
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
    response = web_main.templates.TemplateResponse(
        request=_request(),
        name="ticker.html",
        context=web_main.page_context(
            _request(),
            None,
            detail=detail,
            comments=[],
            comment_count=0,
            active_call=None,
            calls=[],
            latest_commission=None,
            active_tab="pulse",
        ),
    )
    html = response.body.decode()
    styles = "\n".join(
        (ROOT / "web/static" / name).read_text()
        for name in (
            "mobile.css",
            "enhancements.css",
            "ticker-row.css",
            "sleek.css",
            "desktop-split.css",
            "product-system.css",
        )
    )
    html = html.replace("</head>", f"<style>{styles}</style></head>")
    html = re.sub(r'<link rel="stylesheet"[^>]*>', "", html)
    return re.sub(
        r"<script[^>]*>.*?</script>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )


def test_model_path_receipt_stays_compact_and_keeps_risk_separate(page: Page) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.set_content(_rendered_ticker(), wait_until="domcontentloaded")

    card = page.locator(".model-path-card")
    risk = page.locator(".risk-decision")
    assert card.is_visible()
    assert [
        " ".join(text.split())
        for text in card.locator(".model-path-outcomes > div").all_inner_texts()
    ] == ["15% −4% first", "31% No barrier", "54% +8% first"]
    assert card.bounding_box()["height"] < 170
    assert risk.bounding_box()["y"] > card.bounding_box()["y"] + card.bounding_box()["height"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth") is True
