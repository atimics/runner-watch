from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from runner_web import main as web


def test_chart_batch_pages_cover_the_whole_board_without_per_row_calls(monkeypatch):
    limits, batches = [], []

    def pulse_data(*, offset, limit):
        limits.append((offset, limit))
        rows = [{"ticker": f"STK{i}"} for i in range(offset, min(offset + limit, 120))]
        return {
            "rows": rows,
            "next_offset": offset + len(rows),
            "has_more": offset + len(rows) < 120,
        }

    def chart_payload(tickers):
        batches.append(tickers)
        return {"charts": {ticker: [] for ticker in tickers}}

    monkeypatch.setattr(web, "pulse_data", pulse_data)
    monkeypatch.setattr(web, "ticker_charts_payload", chart_payload)
    monkeypatch.setattr(web, "enforce_rate", lambda *args, **kwargs: None)
    request = Request({"type": "http", "method": "GET", "path": "/api/pulse/charts", "headers": []})
    pages = [
        json.loads(asyncio.run(web.pulse_charts_api(request, offset=offset)).body)
        for offset in (0, 50, 100)
    ]
    assert limits == [(0, 50), (50, 50), (100, 50)]
    assert [len(batch) for batch in batches] == [50, 50, 20]
    assert [len(page["charts"]) for page in pages] == [50, 50, 20]
    assert [(page["next_offset"], page["has_more"]) for page in pages] == [
        (50, True),
        (100, True),
        (120, False),
    ]


def test_warmer_matches_board_chart_batch_and_keeps_five_detail_warms(monkeypatch):
    batches, details = [], []
    rows = [{"ticker": f"STK{i}"} for i in range(70)]
    monkeypatch.setattr(web, "_pulse_base_data", lambda: {"rows": rows})
    monkeypatch.setattr(web, "ticker_charts_payload", batches.append)
    monkeypatch.setattr(web, "_public_ticker_detail_data", details.append)
    web._warm_list_charts()
    assert batches == [[f"STK{i}" for i in range(50)]]
    assert details == [f"STK{i}" for i in range(5)]
