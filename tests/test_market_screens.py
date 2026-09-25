from __future__ import annotations

import html as html_lib
import json
from datetime import UTC, datetime, timedelta

import pytest
from starlette.requests import Request

from runner_web import main as web
from runner_web.market_screens import detail, listing, series

SENTINEL = "operator-only-secret"


@pytest.mark.parametrize(
    "market_chance,tag,side", [(0.7, "ABOVE", "NYK"), (0.5, "ABOVE", "BOS"), (0.6, "LEVEL", "BOS")]
)
def test_sports_row_tag_follows_the_same_outcome_as_the_glyph(market_chance, tag, side):
    now = datetime.now(UTC)
    event = sample("sports")
    event.update(status="pre", start_time=(now + timedelta(days=1)).isoformat())
    event["prediction"] = {
        "home_probability": 0.4,
        "away_probability": 0.6,
        "selection": "home",
        "signal": "watch",
        "model_version": "team-form-v1",
        "observed_at": now.isoformat(),
    }
    event["prediction_markets"] = [
        {
            "source": "kalshi",
            "away_probability": market_chance,
            "home_probability": 1 - market_chance,
            "observed_at": now.isoformat(),
            "quality": "quoted",
        }
    ]
    board = listing("sports", [event])
    displayed = board["rows"][0]
    assert displayed["ticker"]["selected"]["label"] == side
    assert displayed["tag"] == tag
    assert displayed["tag_title"].startswith(f"{side}: RATi {tag.lower()} Kalshi")
    assert board["counts"] == {tag.lower(): 1}
    event["prediction_markets"][0]["quality"] = "wide spread"
    assert listing("sports", [event])["rows"][0]["tag"] == "MODEL"


def test_value_row_pins_tb_comparison_through_navigation_and_refresh():
    from urllib.parse import parse_qsl, urlsplit

    now = datetime.now(UTC)
    event = sample("sports")
    event.update(
        id="mlb:401817078",
        league="mlb",
        away_abbreviation="TB",
        home_abbreviation="PHI",
        start_time=(now + timedelta(hours=1)).isoformat(),
        prediction={
            "model_version": "team-form-v1",
            "observed_at": now.isoformat(),
            "away_probability": 0.498,
            "home_probability": 0.502,
        },
        prediction_markets=[
            {
                "source": "kalshi",
                "observed_at": now.isoformat(),
                "away_probability": 0.391,
                "home_probability": 0.609,
            }
        ],
    )
    board = listing("sports", [event])
    item = board["rows"][0]
    assert item["href"] == "/game/mlb:401817078?contract=winner&outcome=away"
    assert "TB · +10.7 points vs market" in render(board)
    selected = dict(parse_qsl(urlsplit(item["href"]).query))
    screen = detail("sports", event, **selected)
    assert screen["ticker"]["selected"]["label"] == "TB"
    assert screen["ticker"]["indicator"]["sentiment"] == "positive"
    assert "TB · RATi − market" in render(screen)
    refresh = dict(parse_qsl(urlsplit(screen["refresh_url"]).query))
    # A price move changes the gap while the reader stays on the chosen team.
    event["prediction_markets"][0].update(away_probability=0.55, home_probability=0.45)
    assert detail("sports", event)["ticker"]["selected"]["label"] == "PHI"
    refreshed = detail("sports", event, **refresh)
    assert refreshed["ticker"]["selected"]["label"] == "TB"
    assert refreshed["item"]["tag"] == "BELOW"
    assert refreshed["item"]["ticker"]["indicator"]["sentiment"] == "negative"


def sample(market):
    if market == "sports":
        return {
            "id": "nba:123",
            "away_abbreviation": "BOS",
            "home_abbreviation": "NYK",
            "away_team_name": "Boston Celtics",
            "home_team_name": "New York Knicks",
            "league": "nba",
            "status": "in",
            "away_score": 72,
            "home_score": 68,
            "start_time": "2026-09-12T18:00:00Z",
            "view_state": {
                "started": True,
                "score_available": True,
                "label": "Live",
                "pick_state": "closed",
            },
            "source_error": SENTINEL,
            "receipt": {"logs": SENTINEL},
        }
    if market == "memecoins":
        return {
            "id": "solana-test",
            "symbol": "BONK",
            "name": "Bonk",
            "price": 0.000018,
            "change_24h": 4.2,
            "observed_at": datetime.now(UTC).isoformat(),
            "source_url": SENTINEL,
            "discovery": {"slot": SENTINEL},
            "token_address": SENTINEL,
        }
    return {
        "ticker": "OPK",
        "company": "OPKO Health",
        "price": 1.56,
        "change_pct": 5.4,
        "quote_time": datetime.now(UTC).isoformat(),
        "source": SENTINEL,
        "directional_thesis": {"model": SENTINEL},
    }


def render(screen, user=None):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", b"app.test")],
            "scheme": "http",
            "server": ("app.test", 80),
        }
    )
    request.state.csp_nonce = "test"
    return web.templates.TemplateResponse(
        request,
        "market_screen.html",
        {
            "request": request,
            "screen": screen,
            "user": user,
            "runners_origin": "http://app.test",
            "sports_origin": "http://sports.test",
            "static_version": "test",
        },
    ).body.decode()


def test_memecoin_screen_shows_quote_and_saved_chain_evidence_without_score():
    coin = {**sample("memecoins"), "token_address": "token-a"}
    board = render(listing("memecoins", [coin]))
    assert ">Market quote</small>" in board
    assert ">Pending</small>" not in board
    assert "Action tags require a saved Runner assessment." in board

    finding = {
        "token_address": "token-a",
        "kind": "liquidity_withdrawal",
        "title": "Pool liquidity withdrawal",
        "source_url": "https://example.test/tx/1",
        "observed_at": "2026-09-19T12:00:00Z",
    }
    coin["findings"] = [finding]
    board = render(listing("memecoins", [coin]))
    opened = render(detail("memecoins", {"coin": coin}))
    assert ">Chain evidence</small>" in board
    assert 'aria-label="Token evidence"' in opened
    assert "Pool liquidity withdrawal" in opened
    assert 'href="https://example.test/tx/1"' in opened


@pytest.mark.parametrize("market", ["stocks", "memecoins", "sports"])
@pytest.mark.parametrize("view", ["list", "map"])
def test_shared_board_only_renders_business_fields(market, view):
    screen = listing(market, [sample(market)], view=view)
    html = render(screen)
    assert SENTINEL not in html
    assert "private-actor" not in html
    assert 'aria-label="View"' not in html
    assert ">Map</a>" not in html
    assert 'class="ticker-list market-' in html
    assert "screenData" not in html
    assert "desktop-workspace" not in html
    assert html_lib.escape(screen["rows"][0]["href"]) in html


def scored_stock(**extra):
    return {
        **sample("stocks"),
        "trade_state": "TRIGGERED",
        "stage": "RUNNING",
        "rug_level": "low",
        "score": 84.0,
        "score_detail": {
            "score": 84.0,
            "drivers": [{"key": "market", "label": "Market scanner", "value": 60.0}],
            "penalties": [{"key": "rug", "label": "Rug risk", "value": -12.0}],
        },
        **extra,
    }


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"trade_state": "TRIGGERED"}, "RUNNING"),
        ({"stage": "RUNNING"}, "RUNNING"),
        ({"trade_state": "MANAGE"}, "RUNNING"),
        ({"trade_state": "ARMED"}, "SETUP"),
        ({"stage": "EARLY"}, "SETUP"),
        ({"stage": "BUILDING"}, "SETUP"),
        ({"stage": "EXTENDED"}, "EXTENDED"),
        ({"trade_state": "AVOID"}, "AVOID"),
        ({"trade_state": "EXIT"}, "AVOID"),
        ({"rug_level": "high"}, "AVOID"),
        ({"rug_level": "critical"}, "AVOID"),
        ({"trade_state": "WATCH"}, "WATCH"),
        ({}, ""),
    ],
)
def test_action_tag_precedence(state, expected):
    screen = listing("stocks", [{**sample("stocks"), **state}])
    assert screen["rows"][0]["tag"] == expected


def test_list_exposes_score_and_breakdown_without_internal_fields():
    screen = listing("stocks", [scored_stock()])
    row = screen["rows"][0]
    assert row["score"] == 84.0
    assert row["tag"] == "RUNNING"
    assert row["score_detail"]["drivers"][0]["label"] == "Market scanner"
    html = render(screen)
    assert "ticker-score" in html
    assert SENTINEL not in html


def test_old_assessment_price_is_explained_on_list_and_detail():
    item = scored_stock(
        trade_state="WATCH",
        stage="WATCH",
        eligibility={
            "state": "unknown",
            "reasons": [{"code": "stale_quote", "label": "Quote is not current"}],
        },
        eligibility_note="Quote is not current",
        quote_as_of="2026-09-23T14:15:00Z",
        quote_time="2026-09-23T14:38:00Z",
    )
    board = listing("stocks", [item])
    assert board["stale_assessments"] == 1
    assert board["rows"][0]["tag"] == "PAUSED"
    assert "1 stock assessment uses an older price" in render(board)

    screen = detail("stocks", {"ticker": "OPK", "company": "OPKO Health", "current": item})
    html = render(screen)
    assert "PAUSED" in html
    assert "Quote is not current" in html
    assert "Assessment used price from Sep 23 · 14:15 UTC" in html
    assert "Sep 23 · 14:38 UTC" in html


def test_stock_search_keeps_the_board_chart_page_offset():
    items = [{**sample("stocks"), "ticker": f"STK{i}"} for i in range(53)]
    screen = listing("stocks", items, query="STK52")
    assert screen["rows"][0]["chart_offset"] == 50
    assert 'data-chart-offset="50"' in render(screen)


def test_stock_glyph_uses_attention_contributions_not_penalty_slices():
    screen = listing("stocks", [scored_stock()])
    html = render(screen)
    assert "conic-gradient(var(--indicator-market) 0.000000% 100.000000%)" in html
    assert "Rug risk -12.0" not in html
    assert 'data-risk="unknown"' in html
    missing = listing(
        "stocks",
        [
            scored_stock(
                score=50,
                score_detail={
                    "drivers": [{"key": "market", "value": 0}],
                    "penalties": [{"key": "rug", "value": -12}],
                },
            )
        ],
    )
    assert 'data-mix="unknown"' in render(missing)
    assert "conic-gradient(" not in render(missing)


def test_public_score_detail_uses_short_driver_labels():
    detail = web._public_score_detail(
        {
            "market": 5,
            "sec_event": 0,
            "news": 0,
            "social_search": 0,
            "community": 0,
            "rug": -3,
        },
        2,
    )
    assert [part["label"] for part in detail["drivers"]] == [
        "Scan",
        "SEC",
        "News",
        "Social",
        "Cluster holdings",
        "Community",
    ]
    assert [part["label"] for part in detail["penalties"]] == ["Rug"]


def test_memecoin_pause_state_gets_a_tag():
    screen = listing("memecoins", [{**sample("memecoins"), "stale": True}])
    assert screen["rows"][0]["tag"] == "PAUSED"


def test_updated_label_and_counts_reach_the_top_bar():
    screen = listing(
        "stocks",
        [scored_stock(ticker="AAA"), sample("stocks")],
        updated_at=datetime.now(UTC).isoformat(),
    )
    assert screen["updated_label"] == "now"
    assert screen["counts"] == {"running": 1}


@pytest.mark.parametrize("market", ["stocks", "memecoins", "sports"])
def test_detail_payload_excludes_internal_fields(market):
    raw = sample(market)
    data = (
        {"coin": raw, "can_call": False, "history": []}
        if market == "memecoins"
        else {"ticker": "OPK", "company": "OPKO Health", "current": raw}
        if market == "stocks"
        else raw
    )
    screen = detail(market, data)
    assert SENTINEL not in json.dumps(screen)
    assert SENTINEL not in render(screen)
    assert screen["kind"] == "detail"
    assert len(screen["facts"]) <= 2


def test_sparse_chart_retains_real_samples_only():
    assert series(
        [
            {"time": "2026-09-12T12:00:00Z", "close": 1.5, "source": SENTINEL},
            {"time": "2026-09-12T12:05:00Z", "close": float("nan")},
            {"close": 1.6},
        ]
    ) == [{"time": "2026-09-12T12:00:00Z", "value": 1.5}]


def test_coin_actions_follow_quote_availability():
    data = {"coin": sample("memecoins"), "can_call": False}
    assert detail("memecoins", data)["actions"] == []
    data["can_call"] = True
    active = {"public_id": "call-1", "return_pct": 3.2}
    action = detail("memecoins", data, active_call=active)["actions"][0]
    assert action["endpoint"] == "/api/memecoin-calls/call-1/close"


def test_sports_closed_game_has_score_and_existing_call():
    screen = detail("sports", sample("sports"), my_pick={"result": "win"})
    assert screen["actions"] == []
    assert screen["teams"][0]["score"] == "72"
    assert screen["call"]["outcome"] == "Win"


def _render_template(name, context):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [(b"host", b"app.test")],
            "scheme": "http",
            "server": ("app.test", 80),
        }
    )
    request.state.csp_nonce = "test"
    return web.templates.env.get_template(name).render(
        request=request,
        user=None,
        runners_origin="http://app.test",
        sports_origin="http://sports.test",
        static_version="test",
        **context,
    )


def test_stock_detail_orders_chart_unified_score_map_and_comments():
    detail_data = {
        "ticker": "OPK",
        "company": "OPKO Health",
        "current": {
            **sample("stocks"),
            "score": 84.0,
            "trade_state": "TRIGGERED",
            "stage": "RUNNING",
            "rug_level": "low",
            "score_detail": {
                "score": 84.0,
                "drivers": [{"key": "market", "label": "Market scanner", "value": 60.0}],
                "penalties": [{"key": "rug", "label": "Rug risk", "value": -12.0}],
            },
        },
    }
    html = _render_template(
        "simple_stock_detail.html",
        {
            "detail": detail_data,
            "active_call": None,
            "comments": [],
            "comment_count": 0,
            "comment_generation_enabled": False,
            "calls": [],
        },
    )
    assert SENTINEL not in html
    chart = html.index('class="visual"')
    ticker_map = html.index('class="ticker-map"')
    score = html.index("data-map-selection")
    comments = html.index('class="discussion-section"')
    support = html.index('class="ticker-support"')
    # Reactions sit at the bottom, below the company and community sections.
    assert chart < ticker_map < score < support < comments
    assert 'class="metrics"' not in html
    assert 'class="breakdown"' not in html
    assert "84" in html
    assert "Market scanner" in html


def test_stock_detail_shows_the_robinhood_chain_token_and_disclosure():
    detail_data = {
        "ticker": "P",
        "company": "Everpure",
        "current": {**sample("stocks")},
    }
    token = {
        "symbol": "P",
        "name": "Everpure • Robinhood Token",
        "contract_address": "0x1Cdad396DB64BDa184d5182A97Dd9B3C62100b7D",
        "chain_id": 4663,
        "multiplier": "1.000000000000000000",
        "pending_multiplier": "",
        "status": "active",
        "logo_url": "",
        "docs_url": "https://docs.robinhood.com/chain/stock-tokens",
        "price": {
            "symbol": "P",
            "bid": "213.45",
            "ask": "213.47",
            "currency": "USD",
            "volume": "48293710",
            "halt": False,
            "as_of": "2026-06-23T15:53:30Z",
        },
        "actions": [
            {
                "type": "forward_split",
                "label": "Forward split",
                "date": "2026-06-15",
                "status": "completed",
            }
        ],
    }
    html = _render_template(
        "simple_stock_detail.html",
        {"detail": detail_data, "active_call": None, "calls": [], "robinhood_token": token},
    )
    assert "Robinhood Chain token" in html
    assert token["contract_address"] in html
    assert "Chain" in html and "4663" in html
    assert "213.45 / 213.47" in html
    assert "Forward split (2026-06-15)" in html
    assert "not the underlying stock" in html
    assert token["docs_url"] in html

    plain = _render_template(
        "simple_stock_detail.html",
        {"detail": detail_data, "active_call": None, "calls": []},
    )
    assert "Robinhood Chain token" not in plain


def test_search_applies_to_map_and_list():
    for view in ["list", "map"]:
        assert len(listing("stocks", [sample("stocks")], query="opko", view=view)["rows"]) == 1
        assert listing("stocks", [sample("stocks")], query="missing", view=view)["rows"] == []


@pytest.fixture
def screen_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from runner_web import db

    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "screens.db")
    monkeypatch.setattr(db, "DATABASE_URL", "")
    monkeypatch.setattr(db, "REQUIRE_DATABASE_URL", False)
    db.init_db()
    monkeypatch.setattr(web, "enforce_rate", lambda *a, **kw: None)
    monkeypatch.setattr(web, "current_user", lambda *a: None)
    monkeypatch.setattr(web, "_public_screen_data", lambda kind, key, build, **options: build())
    client = TestClient(web.app)
    yield client
    client.close()


@pytest.mark.parametrize(
    "path", ["/memecoins", "/memecoins?view=map", "/memecoins/coin/solana-test"]
)
def test_coin_routes_preserve_paused_price_and_recover(screen_client, monkeypatch, path):
    coin = {**sample("memecoins"), "stale": True}
    monkeypatch.setattr(web, "memecoin_market", lambda **kw: {"rows": [coin]})
    monkeypatch.setattr(web, "market_actor_map", lambda *a: {})
    monkeypatch.setattr(
        web,
        "_memecoin_detail_payload",
        lambda *a: {"coin": coin, "calls": [], "can_call": not coin["stale"]},
    )
    for stale in (True, False, True):
        coin["stale"] = stale
        response = screen_client.get(path)
        assert response.status_code == 200
        assert ("Price paused" in response.text) == stale
        assert SENTINEL not in response.text
        quote = screen_client.get("/api/screens/memecoins/solana-test/quote").json()
        assert quote["freshness"] == ("paused" if stale else "current")
        assert quote["change"] == ("Price paused" if stale else "+4.2%")
        assert quote["tone"] == ("neutral" if stale else "up")
        assert quote["time"]
        assert SENTINEL not in json.dumps(quote)


@pytest.mark.parametrize("path", ["/?league=nba", "/?league=nba&view=map", "/game/nba:123"])
def test_sports_routes_follow_confirmed_scores(screen_client, monkeypatch, path):
    from runner_web.sports import _game_view_state

    event = sample("sports")
    monkeypatch.setattr(web, "product_for_request", lambda *a: "sports")
    monkeypatch.setattr(web, "sports_slate", lambda *a: {"events": [event]})
    monkeypatch.setattr(web, "sports_event", lambda *a: event)
    for status, now, away, home, expected in [
        ("pre", "2026-09-12T17:00:00+00:00", 0, 0, "Sep 12 · 18:00 UTC"),
        ("pre", "2026-09-12T19:00:00+00:00", 0, 0, "Score pending"),
        ("in", "2026-09-12T19:01:00+00:00", None, None, "Score pending"),
        ("in", "2026-09-12T19:02:00+00:00", 0, 0, "0 – 0"),
        ("in", "2026-09-12T19:03:00+00:00", 72, 68, "72 – 68"),
        ("post", "2026-09-12T21:00:00+00:00", 102, 98, "102 – 98"),
    ]:
        event.update(status=status, away_score=away, home_score=home)
        event["view_state"] = _game_view_state(event, datetime.fromisoformat(now))
        response = screen_client.get(path)
        assert response.status_code == 200
        assert expected in response.text
        assert SENTINEL not in response.text
        screen = detail("sports", event)
        if not event["view_state"]["score_available"]:
            assert "0 – 0" not in response.text
            assert all(team["score"] == "—" for team in screen["teams"])
        else:
            assert screen["teams"][0]["score"] == str(away)


@pytest.mark.parametrize("status,expected", [("pre", "—"), ("in", "0 – 0"), ("post", "0 – 0")])
def test_slate_rows_derive_score_state_when_detail_state_is_absent(status, expected):
    event = sample("sports")
    event.pop("view_state")
    event.update(status=status, away_score=0, home_score=0, start_time="2020-01-01T18:00:00Z")
    screen = detail("sports", event)
    assert screen["item"]["value"] == expected
    assert listing("sports", [event])["rows"][0] == screen["item"]
    assert all(team["score"] == ("—" if status == "pre" else "0") for team in screen["teams"])
    if status == "pre":
        assert screen["item"]["change"] == "Score pending"


@pytest.fixture
def changing_detail(screen_client, monkeypatch):
    from runner_web.db import connection

    with connection() as database:
        database.execute(
            "INSERT INTO users(id,username,display_name,status,created_at) VALUES(?,?,?,?,?)",
            ("reader", "reader", "Reader", "active", datetime.now(UTC).isoformat()),
        )
    state = {"price": 1.56, "paused": False, "active": True}
    user = {"id": "reader", "username": "reader", "display_name": "Reader"}
    monkeypatch.setattr(web, "current_user", lambda *a: user)
    monkeypatch.setattr(web, "require_user", lambda *a: user)
    monkeypatch.setattr(web, "require_origin", lambda *a: None)
    monkeypatch.setattr(web, "_known_ticker", lambda *a: True)

    def coin_data(*a):
        coin = {**sample("memecoins"), "price": state["price"], "stale": state["paused"]}
        return {"coin": coin, "history": [], "can_call": not state["paused"], "calls": []}

    def stock_data(*a):
        return {
            "ticker": "OPK",
            "company": "OPKO Health",
            "can_publish": True,
            "current": {**sample("stocks"), "price": 1.56},
        }

    def active(*a, **kw):
        return (
            {
                "public_id": "call-one",
                "entry_price": 1.5,
                "return_pct": 4,
                "status": "active",
                "ticker": "OPK",
            }
            if state["active"]
            else None
        )

    def mark(*a, **kw):
        return {
            "price": state["price"],
            "observed_at": datetime.now(UTC).isoformat(),
            "source": SENTINEL,
            "age_seconds": 0,
            "session": "regular",
        }

    monkeypatch.setattr(web, "ticker_detail_data", stock_data)
    monkeypatch.setattr(web, "_public_ticker_detail_data", stock_data)
    monkeypatch.setattr(web, "ticker_quote", lambda *a, **k: {})
    monkeypatch.setattr(web, "market_mark", mark)
    monkeypatch.setattr(web, "_current_call_mark", mark)
    monkeypatch.setattr(
        web,
        "ticker_chart_detail_payload",
        lambda *a: {
            "points": [
                {"time": "2026-09-12T12:00:00Z", "close": 1.5},
                {"time": "2026-09-12T13:00:00Z", "close": state["price"]},
            ]
        },
    )
    monkeypatch.setattr(web, "_memecoin_detail_payload", coin_data)
    monkeypatch.setattr(web, "active_call_for_user", active)
    monkeypatch.setattr(web, "active_memecoin_call", active)
    # The actual page routes retain their existing account context builders.
    monkeypatch.setattr(web, "wallet_for_user", lambda *a: {"balance": 100})
    return state


@pytest.mark.parametrize("market,subject", [("stocks", "OPK"), ("memecoins", "solana-test")])
def test_detail_refresh_uses_one_quote_for_return_and_actions(
    screen_client, changing_detail, market, subject
):
    endpoint = f"/api/screens/{market}/{subject}/detail"
    for price in (3, 4.5):
        changing_detail["price"] = price
        response = screen_client.get(endpoint)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        screen = response.json()
        assert screen["item"]["value"] == f"${price:.2f}"
        assert screen["call"]["return"] == ("+100.0%" if price == 3 else "+200.0%")
        assert screen["actions"][0]["body"] == {"expected_price": price}
        assert screen["actions"][0]["label"] == "Close Call"
        assert SENTINEL not in response.text
    changing_detail["active"] = False
    screen = screen_client.get(endpoint).json()
    assert screen["call"]["status"] == "none"
    assert screen["actions"][0]["label"] == "Make Call"
    if market == "memecoins":
        changing_detail["paused"] = True
        assert screen_client.get(endpoint).json()["actions"] == []
        changing_detail["paused"] = False
        assert screen_client.get(endpoint).json()["actions"][0]["label"] == "Make Call"


@pytest.mark.parametrize("closing", [False, True])
def test_stock_commit_checks_the_price_it_will_save(
    screen_client, changing_detail, monkeypatch, closing
):
    saved = []
    monkeypatch.setattr(web, "create_call", lambda *a, **kw: saved.append(kw) or {})
    monkeypatch.setattr(
        web, "close_call", lambda *a, **kw: saved.append(kw) or {"status": "closed"}
    )
    monkeypatch.setattr(web, "call_for_user", lambda *a: {"status": "active", "ticker": "OPK"})
    endpoint = "/api/calls/stock/call-one/close" if closing else "/api/calls/stock/OPK"
    changing_detail["price"] = 3
    assert screen_client.post(endpoint, json={"expected_price": 1.56}).status_code == 409
    assert saved == []
    assert screen_client.post(endpoint, json={"expected_price": 3}).status_code in (200, 201)
    assert saved[0]["exit_price" if closing else "entry_price"] == 3


@pytest.mark.parametrize("price", [None, True, "3", -1, 0])
def test_call_preview_price_has_a_strict_numeric_shape(screen_client, changing_detail, price):
    assert (
        screen_client.post("/api/calls/stock/OPK", json={"expected_price": price}).status_code
        == 422
    )


def test_map_cached_portraits_read_saved_images_only(screen_client, monkeypatch):
    def unexpected_generation(*a, **kw):
        pytest.fail("Map browsing must use saved portraits")

    monkeypatch.setattr(web, "generate_actor_portrait", unexpected_generation)
    monkeypatch.setattr(web, "portrait_for_actor", lambda *a: None)
    url = "/api/market-actors/actor-one/portrait?cached=true"
    assert screen_client.get(url).status_code == 404
    monkeypatch.setattr(
        web, "portrait_for_actor", lambda *a: {"bytes": b"saved-image", "content_type": "image/png"}
    )
    response = screen_client.get(url)
    assert response.status_code == 200
    assert response.content == b"saved-image"
