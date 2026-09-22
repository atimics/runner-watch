from __future__ import annotations

import asyncio
import json

from starlette.requests import Request

from runner_web import main as web


def test_chart_batch_covers_all_fifty_board_candidates_without_per_row_calls(monkeypatch):
    limits, batches = [], []

    def pulse_data(*, limit):
        limits.append(limit)
        return {"rows": [{"ticker": f"STK{i}"} for i in range(limit)]}

    def chart_payload(tickers):
        batches.append(tickers)
        return {"charts": {ticker: [] for ticker in tickers}}

    monkeypatch.setattr(web, "pulse_data", pulse_data)
    monkeypatch.setattr(web, "ticker_charts_payload", chart_payload)
    monkeypatch.setattr(web, "enforce_rate", lambda *args, **kwargs: None)
    request = Request({"type": "http", "method": "GET", "path": "/api/pulse/charts", "headers": []})
    response = asyncio.run(web.pulse_charts_api(request))
    assert limits == [50]
    assert len(batches) == 1 and len(batches[0]) == 50
    assert len(json.loads(response.body)["charts"]) == 50


def test_warmer_matches_board_chart_batch_and_keeps_five_detail_warms(monkeypatch):
    batches, details = [], []
    rows = [{"ticker": f"STK{i}"} for i in range(70)]
    monkeypatch.setattr(web, "_pulse_base_data", lambda: {"rows": rows})
    monkeypatch.setattr(web, "ticker_charts_payload", batches.append)
    monkeypatch.setattr(web, "_public_ticker_detail_data", details.append)
    web._warm_list_charts()
    assert batches == [[f"STK{i}" for i in range(50)]]
    assert details == [f"STK{i}" for i in range(5)]
