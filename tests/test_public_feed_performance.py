from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request

from runner_web import main as web_main


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/pulse",
            "headers": headers or [],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _sports_event(event_id: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "away_abbreviation": "AWY",
        "away_team_name": "Away Club",
        "home_abbreviation": "HME",
        "home_team_name": "Home Club",
        "away_score": 1,
        "home_score": 2,
        "league": "mlb",
        "start_time": "2026-08-28T20:00:00+00:00",
        "status_detail": "Top 4th",
        "signal_abbreviation": "HME",
        "signal_coin_tone": 2,
        "model_probability_pct": 56.0,
        "market_probability_pct": 48.0,
        "model_winner_abbreviation": "HME",
        "model_winner_probability_pct": 56.0,
        "bovada_divergence_material": True,
        "bovada_divergence_pct": 3.1,
        "bovada_divergence_team": "HME",
        "prediction": {"edge_pct": 8.0, "evidence": ["x" * 4_000]},
        "edge_history": {
            "label": "Edge history",
            "plot_points": "2,12 90,4",
            "dot_x": 90,
            "dot_y": 4,
            "points": [{"edge_pct": 5.0}, {"edge_pct": 8.0}],
            "raw": "x" * 4_000,
        },
        "radar_kind": "market",
        "radar_label": "PRICE",
        "radar_value": 2.0,
        "radar_detail": "Market moved toward HME.",
        "market_comparison": {"books": ["x" * 4_000]},
        "latest_news": {"headline": "x" * 4_000},
        "series_more": [],
    }


def test_runner_public_feed_omits_detail_only_fields(monkeypatch) -> None:
    row = {
        "ticker": "ONE",
        "company": "One Corp",
        "price": 1.25,
        "change_pct": 9.5,
        "section": "scored",
        "trade_state": "EARLY",
        "coin_tone": 2,
        "coin_label": "ON",
        "entered_at": "2026-08-28T15:00:00+00:00",
        "event_count": 2,
        "rug_score": 12.0,
        "rug_level": "low",
        "sentiment": "positive",
        "pulse_label": "Form 4 · CEO buy",
        "case_thesis": "Management buying supports the case.",
        "issuer_risk_json": "x" * 8_000,
        "score_components": {"raw": "x" * 8_000},
        "kol_calls": [{"reason": "x" * 8_000}],
    }
    base = {
        "rows": [row],
        "stats": {"live": 1},
        "flash_record": None,
        "updated_at": "2026-08-28T15:00:00+00:00",
    }
    monkeypatch.setattr(web_main, "_pulse_base_data", lambda: base)

    full = web_main.pulse_data(limit=20)
    public = web_main._public_pulse_data(limit=20)

    public_row = public["rows"][0]
    assert public_row["ticker"] == "ONE"
    assert public_row["pulse_label"] == "Form 4 · CEO buy"
    assert public_row["case_thesis"] == "Management buying supports the case."
    assert "issuer_risk_json" not in public_row
    assert "score_components" not in public_row
    assert "kol_calls" not in public_row
    assert len(json.dumps(public)) < len(json.dumps(full)) * 0.2


def test_sports_public_feeds_keep_only_list_rendering_fields() -> None:
    lead = _sports_event("game-1")
    lead["series_more"] = [_sports_event("game-2")]
    payload = {
        "events": [lead],
        "display_count": 1,
        "model_record": {
            "games": 12,
            "sample": {"target": 250, "message": "x" * 4_000},
            "receipts": ["x" * 4_000],
        },
    }

    pulse = web_main._compact_sports_feed(payload, radar=False)
    radar = web_main._compact_sports_feed(payload, radar=True)

    pulse_event = pulse["events"][0]
    assert pulse_event["prediction"] == {"edge_pct": 8.0}
    assert pulse_event["edge_history"]["point_count"] == 2
    assert "points" not in pulse_event["edge_history"]
    assert "market_comparison" not in pulse_event
    assert "latest_news" not in pulse_event
    assert "signal_coin_tone" not in pulse_event
    assert pulse_event["series_more"][0]["id"] == "game-2"
    assert pulse["model_record"] == {"games": 12, "sample": {"target": 250}}
    assert radar["events"][0]["radar_detail"] == "Market moved toward HME."
    assert "prediction" not in radar["events"][0]
    assert len(json.dumps(pulse)) < len(json.dumps(payload)) * 0.25


def test_sports_feed_cache_is_shared_across_view_and_limit(monkeypatch) -> None:
    built: list[tuple[str, str, int]] = []
    cache: dict[tuple[str, str], dict[str, Any]] = {}

    def screen_data(scope: str, identity: str, builder):
        key = (scope, identity)
        if key not in cache:
            cache[key] = builder()
        return cache[key]

    def pulse(league: str, view: str, limit: int) -> dict[str, Any]:
        built.append((league, view, limit))
        return {
            "events": [_sports_event(f"game-{index}") for index in range(3)],
            "display_count": 3,
            "model_record": {"games": 12, "sample": {"target": 250}},
        }

    monkeypatch.setattr(web_main, "_public_screen_data", screen_data)
    monkeypatch.setattr(web_main, "sports_pulse", pulse)
    monkeypatch.setattr(web_main, "sports_pick_stats", lambda: {"settled": 0})

    first = web_main._public_sports_pulse_data("mlb", view="all", limit=1)
    second = web_main._public_sports_pulse_data("mlb", view="random", limit=2)

    assert built == [("mlb", "signals", 100)]
    assert len(first["pulse"]["events"]) == 1
    assert len(second["pulse"]["events"]) == 2


def test_public_json_uses_etag_for_bodyless_revalidation() -> None:
    first = web_main._conditional_json_response(_request(), {"rows": [{"ticker": "ONE"}]})
    etag = first.headers["etag"]
    repeated = web_main._conditional_json_response(
        _request([(b"if-none-match", etag.encode())]),
        {"rows": [{"ticker": "ONE"}]},
    )

    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=15, must-revalidate"
    assert etag.startswith('W/"')
    assert repeated.status_code == 304
    assert repeated.body == b""
    assert repeated.headers["etag"] == etag
