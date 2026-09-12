"""Small public view models shared by every market screen."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

LABELS = {"stocks": "Stocks", "memecoins": "Memecoins", "sports": "Sports"}


def number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def money(value: Any) -> str:
    n = number(value)
    if n is None:
        return "—"
    if abs(n) >= 1:
        return f"${n:,.2f}"
    return "$" + (f"{n:.6f}".rstrip("0").rstrip(".") if abs(n) >= 0.000001 else f"{n:.6g}")


def change(value: Any) -> str:
    n = number(value)
    return f"{n:+.1f}%" if n is not None else "—"


def stamp(value: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC).strftime("%b %d · %H:%M UTC")
    except (ValueError, TypeError):
        return ""


def sports_state(item: dict[str, Any]) -> dict[str, Any]:
    from runner_web.sports import _game_view_state

    return item.get("view_state") or _game_view_state(item)


def row(market: str, item: dict[str, Any]) -> dict[str, Any]:
    if market == "sports" and str(item.get("id", "")).startswith("golf:"):
        leader = item.get("leader") or {}
        return {
            "id": str(item["id"]),
            "name": str(item.get("name") or "Tournament"),
            "subtitle": "PGA Tour",
            "value": str(leader.get("score_display") or leader.get("score") or "—"),
            "change": str(item.get("display_status") or item.get("status_detail") or "Upcoming"),
            "tone": "neutral",
            "time": stamp(item.get("start_time")),
            "href": "/game/" + quote(str(item["id"]), safe=":"),
            "mark": "PG",
        }
    if market == "sports":
        away = str(item.get("away_abbreviation") or item.get("away_team_name") or "Away")
        home = str(item.get("home_abbreviation") or item.get("home_team_name") or "Home")
        state = sports_state(item)
        started = state.get("started") or item.get("status") in {"in", "post"}
        scores = [item.get(f"{side}_score") for side in ("away", "home")]
        value = (
            " – ".join(str(int(float(s))) for s in scores)
            if state.get("score_available") and all(number(s) is not None for s in scores)
            else "—"
            if started
            else "vs"
        )
        return {
            "id": str(item["id"]),
            "name": f"{away} · {home}",
            "subtitle": str(item.get("league") or "Sports").upper(),
            "value": value,
            "change": (
                "Score pending"
                if value == "—"
                else stamp(item.get("start_time")) or "Upcoming"
                if not started
                else state.get("label")
                or {"in": "Live", "post": "Final"}.get(item.get("status"), "Upcoming")
            ),
            "tone": "neutral",
            "time": stamp(item.get("start_time")),
            "href": "/game/" + quote(str(item["id"]), safe=":"),
            "mark": away[:2],
        }
    coin = market == "memecoins"
    identifier = str(item.get("id") if coin else item.get("ticker") or "")
    name = str(item.get("symbol") if coin else item.get("ticker") or "")
    move = number(item.get("change_24h") if coin else item.get("change_pct"))
    paused = coin and bool(item.get("stale"))
    return {
        "id": identifier,
        "name": name,
        "subtitle": str(
            item.get("name") if coin else item.get("company") or item.get("name") or ""
        ),
        "value": money(item.get("price")),
        "change": "Price paused" if paused else change(move),
        "tone": "neutral"
        if paused
        else "up"
        if move and move > 0
        else "down"
        if move and move < 0
        else "neutral",
        **({"freshness": "paused" if paused else "current"} if coin else {}),
        "time": stamp(
            item.get("observed_at") if coin else item.get("quote_time") or item.get("event_at")
        ),
        "href": ("/memecoins/coin/" if coin else "/t/") + quote(identifier, safe=""),
        "mark": name[:2],
    }


def listing(
    market: str,
    items: list[dict[str, Any]],
    *,
    view: str = "list",
    query: str = "",
    graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = [row(market, item) for item in items]
    query = query.strip()[:80]
    if query:
        rows = [r for r in rows if query.casefold() in (r["name"] + " " + r["subtitle"]).casefold()]
    screen = {"kind": view, "market": market, "label": LABELS[market], "rows": rows, "query": query}
    if view == "map":
        screen.update(map_connections(market, rows, items, graph or {}))
    return screen


def map_connections(
    market: str, rows: list[dict[str, Any]], items: list[dict[str, Any]], graph: dict[str, Any]
) -> dict[str, Any]:
    """Keep one public node per saved participant and link it to its subjects."""
    from runner_web.market_actors import public_relationship

    selected = {r["id"] for r in rows}
    targets = {r["id"]: r for r in (row(market, item) for item in items)}
    nodes: dict[str, dict[str, Any]] = {}
    if market == "sports":
        for game in items:
            key = str(game["id"])
            if key.startswith("golf:"):
                continue
            for side in ("away", "home"):
                name = game.get(f"{side}_team_name") or game.get(f"{side}_abbreviation")
                identity = game.get(f"{side}_team_id") or name
                if not identity:
                    continue
                actor_id = f"team:{game.get('league') or key.split(':')[0]}:{identity}"
                node = nodes.setdefault(
                    actor_id,
                    {
                        "id": actor_id,
                        "name": str(name or identity),
                        "portrait": "",
                        "label": "Team",
                        "explanation": "This team plays in these games.",
                        "links": [],
                    },
                )
                node["links"].append(
                    {
                        "item": targets[key],
                        "time": stamp(game.get("start_time")),
                        "label": "Game time",
                    }
                )
    else:
        for subject in graph.get("subjects", []):
            key = str(subject["key"])
            targets.setdefault(
                key,
                {
                    "id": key,
                    "name": str(subject.get("label") or key),
                    "href": ("/t/" if market == "stocks" else "/memecoins/coin/")
                    + quote(key, safe=""),
                    "value": "",
                    "change": "",
                    "tone": "neutral",
                },
            )
        actors = {str(actor["id"]): actor for actor in graph.get("actors", [])}
        seen = set()
        for link in graph.get("links", []):
            actor_id, key = str(link["actor_id"]), str(link["subject_key"])
            if actor_id not in actors or key not in targets or (actor_id, key) in seen:
                continue
            seen.add((actor_id, key))
            actor = actors[actor_id]
            node = nodes.setdefault(
                actor_id,
                {
                    "id": actor_id,
                    "name": str(actor.get("name") or "Participant"),
                    "portrait": (
                        f"/api/market-actors/{quote(actor_id, safe='')}/portrait?cached=true"
                    )
                    if actor.get("portrait_ready")
                    else "",
                    "label": "Possible wallet link" if market == "memecoins" else "Participant",
                    "explanation": "Saved wallet activity suggests a possible connection."
                    if market == "memecoins"
                    else "Public filings connect this participant to these tickers.",
                    "links": [],
                },
            )
            node["links"].append(
                {
                    "item": targets[key],
                    "time": stamp(link.get("as_of")),
                    "label": public_relationship(market, link.get("direction")),
                }
            )
    connections = []
    connected = set()
    for node in nodes.values():
        if not any(link["item"]["id"] in selected for link in node["links"]):
            continue
        node["mark"] = "".join(word[0] for word in node["name"].split()[:2]).upper()
        node["links"].sort(
            key=lambda link: (link["item"]["id"] not in selected, link["item"]["name"])
        )
        connected.update(link["item"]["id"] for link in node["links"])
        node["visible_links"] = node["links"][:6]
        node["more_links"] = node["links"][6:]
        connections.append(node)
    connections.sort(key=lambda node: (-len(node["links"]), node["name"], node["id"]))
    return {"connections": connections, "unlinked": [r for r in rows if r["id"] not in connected]}


def series(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean = []
    for p in points:
        price = number(p.get("close", p.get("price")))
        when = p.get("timestamp") or p.get("time") or p.get("observed_at")
        if price is not None and price > 0 and when:
            clean.append({"value": price, "time": str(when)})
    return clean[-360:]


def settlement_terms(market: str, entered: Any) -> str:
    if market == "sports":
        return "Settles on the confirmed game result. A cancelled game is void."
    try:
        moment = datetime.fromisoformat(str(entered).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if market == "stocks":
            from runner_web.data_health import stock_settlement_close

            due = stock_settlement_close(moment)
            return f"Settles after session close · {stamp(due)}. You can close early."
        from runner_web.memecoin_calls import MEMECOIN_CALL_MAX_AGE_DAYS

        due = moment + timedelta(days=MEMECOIN_CALL_MAX_AGE_DAYS)
        return f"Settles after {stamp(due)} at the latest saved price. You can close early."
    except (ValueError, TypeError):
        return (
            "Settles after session close. You can close early."
            if market == "stocks"
            else "Settles after seven days at the latest saved price. You can close early."
        )


def call_record(
    market: str, source: dict[str, Any], saved: dict[str, Any] | None
) -> dict[str, Any]:
    if not saved:
        return {"status": "none"}
    closed = saved.get("status") in {"closed", "settled"} or bool(saved.get("result"))
    if market == "sports":
        side = saved.get("selection")
        team = source.get(f"{side}_team_name") or source.get(f"{side}_abbreviation") or "Team"
        odds = number(saved.get("american_odds"))
        entry = f"Moneyline {int(odds):+d}" if odds is not None else "Moneyline pending"
        outcome = str(saved.get("result") or "Open").title()
        reward = int(saved.get("reward_flash") or 0)
        entered = saved.get("created_at")
        choice = f"{team} wins"
        change_label = ""
    else:
        entry = money(saved.get("entry_price"))
        entered = saved.get("entry_at")
        choice = f"{source.get('ticker') or source.get('symbol') or 'Price'} rises"
        price = number(saved.get("exit_price") if closed else source.get("price"))
        base = number(saved.get("entry_price"))
        current = (price / base - 1) * 100 if price is not None and base and base > 0 else None
        if not closed and source.get("stale"):
            current = None
        change_label = change(current)
        outcome = (
            f"Closed at {money(price)} · {change_label}"
            + (f" · {stamp(saved.get('exit_at'))}" if stamp(saved.get("exit_at")) else "")
            if closed
            else f"Open · {change_label}"
        )
        reward = int(saved.get("flash_reward") or 0)
    return {
        "status": "closed" if closed else "active",
        "choice": choice,
        "entry": entry + (f" · {stamp(entered)}" if stamp(entered) else ""),
        "terms": settlement_terms(market, entered),
        "outcome": outcome,
        "return": change_label,
        "reward": f"{reward} Flash" if closed else "Awarded at settlement",
    }


def detail(
    market: str,
    data: dict[str, Any],
    *,
    active_call: dict[str, Any] | None = None,
    my_pick: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = (
        data.get("coin", {})
        if market == "memecoins"
        else data.get("current", {})
        if market == "stocks"
        else data
    )
    if market == "stocks":
        source = {
            **source,
            "ticker": data.get("ticker"),
            "company": data.get("company") or data.get("name"),
        }
    item = row(market, source)
    result = {
        "kind": "detail",
        "market": market,
        "label": LABELS[market],
        "item": item,
        "actions": [],
        "facts": [],
        "series": [],
        "note": "",
        "query": "",
        "refresh_url": f"/api/screens/{market}/{quote(item['id'], safe='')}/detail",
    }
    identifier = quote(item["id"], safe="")
    if market == "sports" and item["id"].startswith("golf:"):
        result["teams"] = [
            {
                "name": str(player.get("player_name") or "Player"),
                "mark": str(player.get("position") or "—"),
                "score": str(player.get("score_display") or player.get("score") or "—"),
            }
            for player in (data.get("leaderboard") or [])[:2]
        ]
        result["note"] = item["change"]
        if not result["teams"]:
            result["teams"] = [{"name": "Field opens soon", "mark": "PGA", "score": "—"}]
        if data.get("venue"):
            result["facts"].append({"label": "Venue", "value": str(data["venue"])})
        result.pop("refresh_url")
        return result
    if market == "sports":
        state = sports_state(data)
        result["call"] = call_record(market, data, my_pick)
        result["teams"] = [
            {
                "name": str(
                    data.get(f"{s}_team_name") or data.get(f"{s}_abbreviation") or s.title()
                ),
                "mark": str(data.get(f"{s}_abbreviation") or s[:1]).upper(),
                "score": str(int(number(data.get(f"{s}_score")) or 0))
                if state.get("score_available")
                else "—",
            }
            for s in ("away", "home")
        ]
        result["note"] = str(item["change"])
        if data.get("venue"):
            result["facts"].append({"label": "Venue", "value": str(data["venue"])})
        if not my_pick and state.get("pick_state") == "open":
            for side in ("away", "home"):
                odds = number((data.get("paper_odds") or {}).get(f"{side}_odds"))
                team = (
                    data.get(f"{side}_team_name")
                    or data.get(f"{side}_abbreviation")
                    or side.title()
                )
                line = f"{int(odds):+d}" if odds is not None else "pending"
                result["actions"].append(
                    {
                        "label": f"Call {data.get(side + '_abbreviation') or side.title()}",
                        "endpoint": f"/api/calls/game/{identifier}",
                        "body": {
                            "selection": side,
                            **({"expected_odds": int(odds)} if odds is not None else {}),
                        },
                        "preview": (
                            f"Call {team} to win · Moneyline {line}. "
                            + settlement_terms(market, data.get("start_time"))
                        ),
                    }
                )
    else:
        result["series"] = series(data.get("history") or [])
        result["chart_period"] = {
            "start": result["series"][0]["time"] if result["series"] else None,
            "end": result["series"][-1]["time"] if result["series"] else None,
        }
        result["note"] = "Price history"
        if market == "stocks":
            result["chart_url"] = f"/api/screens/stocks/{identifier}/chart"
            result["quote_url"] = f"/api/screens/stocks/{identifier}/quote"
        else:
            result["quote_url"] = f"/api/screens/memecoins/{identifier}/quote"
        volume = source.get("volume_label") if market == "memecoins" else None
        if volume:
            result["facts"].append({"label": "24h volume", "value": volume})
        can_call = (
            bool(data.get("can_call"))
            if "can_call" in data or market == "memecoins"
            else bool(source.get("price"))
        )
        result["call"] = call_record(market, source, active_call)
        if active_call and active_call.get("status") == "closed":
            active_call = None
        if can_call:
            endpoint = (
                (
                    f"/api/memecoin-calls/{quote(str(active_call['public_id']), safe='')}/close"
                    if active_call
                    else f"/api/memecoins/{identifier}/calls"
                )
                if market == "memecoins"
                else (
                    f"/api/calls/stock/{quote(str(active_call['public_id']), safe='')}/close"
                    if active_call
                    else f"/api/calls/stock/{identifier}"
                )
            )
            result["actions"].append(
                {
                    "label": "Close Call" if active_call else "Make Call",
                    "endpoint": endpoint,
                    "body": {"expected_price": number(source.get("price"))},
                    "preview": (
                        f"Close your Call at {money(source.get('price'))}. "
                        "Your result uses this price."
                        if active_call
                        else f"Call {item['name']} to rise from {money(source.get('price'))}. "
                        + settlement_terms(
                            market,
                            source.get("quote_time")
                            or source.get("observed_at")
                            or source.get("event_at"),
                        )
                    ),
                }
            )
    return result
