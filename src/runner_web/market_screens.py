"""Small public view models shared by every market screen."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

LABELS = {"stocks": "Stocks", "memecoins": "Memecoins", "sports": "Sports"}


def number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def money(value: Any) -> str:
    n = number(value)
    if n is None:
        return "—"
    return f"${n:,.2f}" if abs(n) >= 1 else f"${n:.6f}".rstrip("0").rstrip(".")


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


def row(market: str, item: dict[str, Any]) -> dict[str, Any]:
    if market == "sports":
        away = str(item.get("away_abbreviation") or item.get("away_team_name") or "Away")
        home = str(item.get("home_abbreviation") or item.get("home_team_name") or "Home")
        state = item.get("view_state") or {}
        started = state.get("started") or item.get("status") in {"in", "post"}
        scores = [item.get(f"{side}_score") for side in ("away", "home")]
        value = (
            " – ".join(str(int(float(s))) for s in scores)
            if started and all(number(s) is not None for s in scores)
            else "vs"
        )
        return {
            "id": str(item["id"]),
            "name": f"{away} · {home}",
            "subtitle": str(item.get("league") or "Sports").upper(),
            "value": value,
            "change": state.get("label")
            or {"in": "Live", "post": "Final"}.get(item.get("status"), "Upcoming"),
            "tone": "neutral",
            "time": stamp(item.get("start_time")),
            "href": "/game/" + quote(str(item["id"]), safe=":"),
            "mark": away[:2],
        }
    coin = market == "memecoins"
    identifier = str(item.get("id") if coin else item.get("ticker") or "")
    name = str(item.get("symbol") if coin else item.get("ticker") or "")
    move = number(item.get("change_24h") if coin else item.get("change_pct"))
    return {
        "id": identifier,
        "name": name,
        "subtitle": str(
            item.get("name") if coin else item.get("company") or item.get("name") or ""
        ),
        "value": money(item.get("price")),
        "change": change(move),
        "tone": "up" if move and move > 0 else "down" if move and move < 0 else "neutral",
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
        screen["groups"] = map_groups(market, rows, items, graph or {})
    return screen


def map_groups(
    market: str, rows: list[dict[str, Any]], items: list[dict[str, Any]], graph: dict[str, Any]
) -> list[dict[str, Any]]:
    """Each item is a selectable centre; satellites represent saved relationships."""
    subjects = {str(s["key"]): s for s in graph.get("subjects", [])}
    raw = {str(i.get("id")): i for i in items}
    groups = []
    for r in rows:
        subject = subjects.get(r["id"]) or subjects.get(r["name"]) or {}
        count = len(subject.get("actor_ids") or [])
        labels = []
        if market == "sports":
            game = raw.get(r["id"], {})
            labels = [
                str(game.get(f"{side}_abbreviation") or side.title()) for side in ("away", "home")
            ]
            count = 2
        satellites = []
        for i in range(min(count, 12)):
            angle = 2 * math.pi * i / min(count, 12) - math.pi / 2
            satellites.append(
                {
                    "x": round(150 + 104 * math.cos(angle), 2),
                    "y": round(145 + 104 * math.sin(angle), 2),
                    "label": labels[i] if i < len(labels) else "",
                }
            )
        groups.append({"item": r, "satellites": satellites})
    return groups


def series(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean = []
    for p in points:
        price = number(p.get("close", p.get("price")))
        when = p.get("timestamp") or p.get("time") or p.get("observed_at")
        if price is not None and price > 0 and when:
            clean.append({"value": price, "time": str(when)})
    return clean[-360:]


def detail(
    market: str,
    data: dict[str, Any],
    *,
    active_call: dict[str, Any] | None = None,
    report: dict[str, Any] | None = None,
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
    }
    identifier = quote(item["id"], safe="")
    if market == "sports":
        state = data.get("view_state") or {}
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
        result["note"] = str(state.get("label") or item["change"])
        if data.get("venue"):
            result["facts"].append({"label": "Venue", "value": str(data["venue"])})
        if my_pick:
            result["note"] = "Your Call · " + str(my_pick.get("result") or "Open").title()
        elif state.get("pick_state") == "open":
            for side in ("away", "home"):
                result["actions"].append(
                    {
                        "label": f"Call {data.get(side + '_abbreviation') or side.title()}",
                        "endpoint": f"/api/calls/game/{identifier}",
                        "body": {"selection": side},
                    }
                )
    else:
        result["series"] = series(data.get("history") or [])
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
            bool(data.get("can_call")) if market == "memecoins" else bool(source.get("price"))
        )
        if active_call:
            result["facts"].append(
                {"label": "Your Call", "value": change(active_call.get("return_pct"))}
            )
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
                    "body": {},
                }
            )
    if report and report.get("href"):
        result["report_url"] = report["href"]
    return result
