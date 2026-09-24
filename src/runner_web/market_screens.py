"""Small public view models shared by every market screen."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from runner_web.market_assessments import assessment
from runner_web.stock_indicator import memecoin_indicator, stock_indicator

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


def ago(value: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return ""
    seconds = max(0.0, (datetime.now(UTC) - dt.astimezone(UTC)).total_seconds())
    if seconds < 90:
        return "now"
    if seconds < 5400:
        return f"{round(seconds / 60)}m ago"
    if seconds < 129600:
        return f"{round(seconds / 3600)}h ago"
    return f"{round(seconds / 86400)}d ago"


def sports_state(item: dict[str, Any]) -> dict[str, Any]:
    from runner_web.sports import _game_view_state

    return item.get("view_state") or _game_view_state(item)


def sports_matchup(item: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Keep the scoreboard leader separate from the saved model and value side."""
    sides = ("away", "home")
    prediction = item.get("prediction") or {}
    probabilities = [number(prediction.get(f"{side}_probability")) for side in sides]
    if all(p is None or 0 <= p <= 1 for p in probabilities):
        if prediction.get("away_probability") is None and probabilities[1] is not None:
            probabilities[0] = 1 - probabilities[1]
        elif prediction.get("home_probability") is None and probabilities[0] is not None:
            probabilities[1] = 1 - probabilities[0]
    valid_model = all(p is not None and 0 <= p <= 1 for p in probabilities)
    valid_model = valid_model and math.isclose(sum(probabilities), 1, abs_tol=0.001)
    favorite = (
        sides[probabilities.index(max(probabilities))]
        if valid_model and probabilities[0] != probabilities[1]
        else None
    )
    scores = [number(item.get(f"{side}_score")) for side in sides]
    completed = bool(item.get("completed")) or item.get("status") == "post"
    started = bool(state.get("started")) or completed or item.get("status") == "in"
    confirmed = (
        started
        and state.get("score_available")
        and all(score is not None and score >= 0 for score in scores)
    )
    leader = sides[scores.index(max(scores))] if confirmed and scores[0] != scores[1] else None
    emphasis = leader
    emphasis_label = "Winner" if completed else "Leading"
    teams = [
        {
            "side": side,
            "label": str(
                item.get(f"{side}_abbreviation") or item.get(f"{side}_team_name") or side.title()
            ),
            "name": str(
                item.get(f"{side}_team_name") or item.get(f"{side}_abbreviation") or side.title()
            ),
            "emphasized": side == emphasis,
            "emphasis_label": emphasis_label if side == emphasis else "",
        }
        for side in sides
    ]
    forecast = None
    if valid_model:
        index = sides.index(favorite) if favorite else 0
        team = teams[index]
        percent = round(probabilities[index] * 100, 1)
        forecast = {
            "team": team["label"] if favorite else "Even",
            "percent": percent,
            "label": f"{team['label']} {percent:g}%" if favorite else "Even 50–50",
            "description": (
                f"Pregame model: {team['name']} {percent:g}% win chance."
                if favorite
                else "Pregame model: both teams have a 50% win chance."
            )
            + (
                f" Saved {stamp(prediction.get('observed_at'))}."
                if stamp(prediction.get("observed_at"))
                else ""
            ),
            "observed_at": prediction.get("observed_at"),
            "saved_label": stamp(prediction.get("observed_at")),
            "model_version": prediction.get("model_version"),
        }
        trace = prediction.get("factors") or {}
        traced_home = number(trace.get("home_probability_pct")) if isinstance(trace, dict) else None
        if (
            favorite
            and traced_home is not None
            and abs(traced_home - probabilities[1] * 100) < 0.001
        ):
            orientation = 1 if favorite == "home" else -1
            parts = [
                ("Season record", "record", "home_record_delta_pp"),
                ("Venue", "venue", "home_venue_delta_pp"),
                ("Clamp", "clamp", "home_clamp_delta_pp"),
            ]
            factors = [
                {
                    "label": label,
                    "key": key,
                    "value": round(orientation * (number(trace.get(field)) or 0), 6),
                }
                for label, key, field in parts
            ]
            adjustment = sum(part["value"] for part in factors)
            if abs(50 + adjustment - probabilities[index] * 100) < 0.001:
                total = sum(abs(part["value"]) for part in factors)
                offset = 0.0
                running = 50.0
                for part in factors:
                    part["share"] = abs(part["value"]) / total * 100 if total else 0.0
                    part["offset"] = offset
                    part["negative"] = part["value"] < 0
                    part["bar_start"] = max(0.0, min(100.0, min(running, running + part["value"])))
                    part["bar_width"] = max(
                        0.0,
                        min(100.0, max(running, running + part["value"]))
                        - part["bar_start"],
                    )
                    running += part["value"]
                    offset += part["share"]
                forecast["factors"] = factors
                forecast["records"] = {
                    side: trace.get(f"{side}_record") for side in sides
                }
                forecast["source_url"] = trace.get("source_url")
                forecast["description"] += " " + "; ".join(
                    f"{part['label']} {part['value']:+.1f} points"
                    for part in factors
                    if abs(part["value"]) >= 0.05
                ) + "."
        market_chance = (
            number(prediction.get(f"{favorite}_market_probability")) if favorite else None
        )
        if market_chance is not None:
            forecast["market_percent"] = round(market_chance * 100, 1)
            forecast["market_gap_pp"] = round(percent - market_chance * 100, 1)
        selection = prediction.get("selection")
        if selection in sides:
            forecast["value_side"] = teams[sides.index(selection)]["label"]
    return {
        "teams": teams,
        "forecast": forecast,
        "emphasis": emphasis,
        "emphasis_label": emphasis_label if emphasis else "",
        "tied": bool(confirmed and scores[0] == scores[1]),
    }


def row(market: str, item: dict[str, Any]) -> dict[str, Any]:
    if market == "sports" and str(item.get("id", "")).startswith("golf:"):
        from runner_web.sports import _golf_display_status

        leader = item.get("leader") or next(iter(item.get("leaderboard") or []), {})
        match_play = item.get("scoring_format") == "match_play"
        teams = item.get("teams") or []
        event_status = _golf_display_status(item, datetime.now(UTC))
        rating = assessment(market, item)
        saved_tag, saved_tone, saved_risk = state_tag(item)
        if saved_tag:
            rating.update(tag=saved_tag, tag_tone=saved_tone)
        return {
            "id": str(item["id"]),
            "name": str(item.get("name") or "Tournament"),
            "subtitle": (
                " vs ".join(str(team.get("name") or "Team") for team in teams)
                if match_play and teams
                else str(leader.get("player_name") or "PGA Tour")
            ),
            "value": (
                "–".join(str(team.get("points_display") or "0") for team in teams[:2])
                if match_play and len(teams) >= 2
                else str(leader.get("score_display") or leader.get("score") or "—")
            ),
            "change": event_status,
            "event_status": event_status,
            "assessment": rating,
            "research_label": "Team points" if match_play else "Round scores",
            "tone": "neutral",
            "time": stamp(item.get("start_time")),
            "href": "/game/" + quote(str(item["id"]), safe=":"),
            "mark": "PG",
            "tag": rating["tag"],
            "tag_tone": rating["tag_tone"],
            "risk": saved_risk,
            "score": rating["score"],
            "score_detail": rating["score_detail"],
            "score_as_of": rating["score_as_of"],
        }
    if market == "sports":
        away = str(item.get("away_abbreviation") or item.get("away_team_name") or "Away")
        home = str(item.get("home_abbreviation") or item.get("home_team_name") or "Home")
        state = sports_state(item)
        rating = assessment(market, item)
        saved_tag, saved_tone, saved_risk = state_tag(item)
        if saved_tag:
            rating.update(tag=saved_tag, tag_tone=saved_tone)
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
            "matchup": sports_matchup(item, state),
            "subtitle": str(item.get("league") or "Sports").upper(),
            "selected_team_label": str(
                item.get(f"{rating.get('selection')}_abbreviation")
                or rating.get("selected_team")
                or ""
            ),
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
            "event_status": state.get("label") or item.get("status_detail") or "Upcoming",
            "assessment": rating,
            "tag": rating["tag"],
            "tag_tone": rating["tag_tone"],
            "risk": saved_risk,
            "score": rating["score"],
            "score_detail": rating["score_detail"],
            "score_as_of": rating["score_as_of"],
        }
    coin = market == "memecoins"
    identifier = str(item.get("id") if coin else item.get("ticker") or "")
    name = str(item.get("symbol") if coin else item.get("ticker") or "")
    move = number(item.get("change_24h") if coin else item.get("change_pct"))
    paused = coin and bool(item.get("stale"))
    rating = assessment(market, item) if coin else None
    if market == "stocks":
        tag, tag_tone, risk = state_tag(item)
        score = number(item.get("score"))
        score_detail = item.get("score_detail")
    elif paused:
        tag, tag_tone, risk = state_tag(item)
        if not tag:
            tag, tag_tone = "PAUSED", "paused"
        score, score_detail = rating["score"], rating["score_detail"]
    else:
        tag, tag_tone, risk = state_tag(item)
        score, score_detail = rating["score"], rating["score_detail"]
    if rating is not None:
        rating["tag"], rating["tag_tone"], _ = state_tag(item)
    subtitle = str(item.get("name") if coin else item.get("company") or item.get("name") or "")
    address = str(item.get("token_address") or "") if coin else ""
    if not re.fullmatch(r"(?:[1-9A-HJ-NP-Za-km-z]{32,44}|0x[a-fA-F0-9]{40})", address):
        address = ""
    if address:
        subtitle = address[:6] + "…" + address[-4:]
    indicator = (
        stock_indicator(item) if market == "stocks" else memecoin_indicator(item) if coin else None
    )
    policy = item.get("eligibility") if market == "stocks" else None
    assessment_stale = isinstance(policy, dict) and any(
        isinstance(reason, dict) and reason.get("code") == "stale_quote"
        for reason in policy.get("reasons") or []
    )
    assessment_quote = stamp(item.get("quote_as_of")) if market == "stocks" else ""
    return {
        **({"indicator": indicator} if indicator else {}),
        **({"verified": indicator["verification"]["verified"]} if market == "stocks" else {}),
        **(
            {
                "assessment_stale": assessment_stale,
                "assessment_quote_label": (
                    f"Assessment used price from {assessment_quote}" if assessment_quote else ""
                ),
            }
            if market == "stocks"
            else {}
        ),
        "id": identifier,
        "name": name,
        "subtitle": subtitle,
        "value": money(item.get("price")),
        "change": "Price paused" if paused else change(move),
        "tone": "neutral"
        if paused
        else "up"
        if move and move > 0
        else "down"
        if move and move < 0
        else "neutral",
        **(
            {
                "freshness": "paused" if paused else "current",
                "assessment": rating,
                "contract_address": address,
                "full_name": str(item.get("name") or ""),
            }
            if coin
            else {}
        ),
        "time": stamp(
            item.get("observed_at") if coin else item.get("quote_time") or item.get("event_at")
        ),
        "href": ("/memecoins/coin/" if coin else "/stock/") + quote(identifier, safe=""),
        "mark": name[:2],
        "tag": tag,
        "tag_tone": tag_tone,
        "risk": risk,
        "score": score,
        "score_detail": score_detail,
        "score_as_of": item.get("score_as_of"),
        "score_policy": item.get("score_policy"),
        "eligibility_note": item.get("eligibility_note"),
        "attention_urgent": bool(item.get("attention_urgent")),
        "feature_as_of": item.get("feature_as_of"),
        "quote_as_of": item.get("quote_as_of"),
        "computed_at": item.get("computed_at"),
        # Carried by the most recent pulse announcement, so the board can show
        # what the channel was just told.
        "announced": bool(item.get("announced")),
    }


def state_tag(item: dict[str, Any]) -> tuple[str, str, bool]:
    """Collapse stage, trade state and rug level into one action tag.

    Precedence is AVOID > EXTENDED > RUNNING > SETUP > WATCH. Risk is returned
    separately so the row can show one small mark without a second badge.
    """

    policy = item.get("eligibility") or {}
    if policy.get("state") == "blocked" or item.get("hard_veto"):
        return "AVOID", "avoid", True
    if policy.get("state") == "unknown":
        return "PAUSED", "paused", False
    trade = str(item.get("trade_state") or "").upper()
    stage = str(item.get("stage") or "").upper()
    rug = str(item.get("rug_level") or "").lower()
    risky = rug in {"high", "critical"}
    if trade in {"AVOID", "EXIT"} or risky:
        return "AVOID", "avoid", risky
    if stage == "EXTENDED":
        return "EXTENDED", "extended", rug == "guarded"
    if trade in {"TRIGGERED", "MANAGE"} or stage == "RUNNING":
        return "RUNNING", "running", rug == "guarded"
    if trade == "ARMED" or stage in {"EARLY", "BUILDING"}:
        return "SETUP", "setup", rug == "guarded"
    if trade or stage:
        return "WATCH", "watch", rug == "guarded"
    return "", "", False


def _search_text(market: str, source: dict[str, Any], display: dict[str, Any]) -> str:
    """Search original identities without adding raw fields to public view models."""
    text = display["name"] + " " + display["subtitle"]
    if market == "memecoins":
        fields = ("id", "symbol", "name", "token_address")
    elif market == "sports":
        fields = (
            "id",
            "name",
            "venue",
            "league",
            "home_team_name",
            "away_team_name",
            "home_abbreviation",
            "away_abbreviation",
        )
    else:
        return text
    text += " " + " ".join(str(source.get(key) or "") for key in fields)
    if market == "sports" and str(source.get("id") or "").startswith("golf:"):
        text += " " + " ".join(
            str(player.get("player_name") or "") for player in source.get("leaderboard") or []
        )
        text += " " + " ".join(str(team.get("name") or "") for team in source.get("teams") or [])
    return text


def listing(
    market: str,
    items: list[dict[str, Any]],
    *,
    view: str = "list",
    query: str = "",
    graph: dict[str, Any] | None = None,
    updated_at: str = "",
    stories: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the tight list screen. The market-wide map view is retired."""

    _ = view, graph  # Kept so saved links keep working; the map lives on the ticker page now.
    rows = [row(market, item) for item in items]
    if market == "stocks":
        for index, entry in enumerate(rows):
            entry["chart_offset"] = (index // 50) * 50
    query = query.strip()[:80]
    if query:
        rows = [
            r
            for source, r in zip(items, rows, strict=True)
            if query.casefold() in _search_text(market, source, r).casefold()
        ]
    counts: dict[str, int] = {}
    for item in rows:
        tone = str(item.get("tag_tone") or "")
        if tone:
            counts[tone] = counts.get(tone, 0) + 1
    if stories:
        for entry in rows:
            subject_stories = stories.get(str(entry["id"])) or []
            if subject_stories:
                entry["story"] = subject_stories[0]
    return {
        "kind": "list",
        "market": market,
        "label": LABELS[market],
        "rows": rows,
        "query": query,
        "total": len(rows),
        "counts": counts,
        "stale_assessments": (
            sum(bool(entry.get("assessment_stale")) for entry in rows) if market == "stocks" else 0
        ),
        "updated_at": updated_at,
        "updated_label": ago(updated_at) if updated_at else "",
    }


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
        # Only stock Calls have a public share page at /c/{public_id}.
        "public_id": saved.get("public_id") if market == "stocks" else None,
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
            "evidence_gate": data.get("evidence_gate", source.get("evidence_gate")),
        }
    if market == "memecoins":
        source = {**source, "findings": data.get("findings") or source.get("findings") or []}
    item = row(market, source)
    if market == "stocks":
        item["score_trace"] = source.get("score_trace") or {}
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
                "href": (
                    "/team/"
                    + "/".join(
                        quote(str(value), safe="")
                        for value in (
                            data["provider"],
                            data["league"],
                            data[f"{s}_team_id"],
                        )
                    )
                    if data.get("provider") and data.get("league") and data.get(f"{s}_team_id")
                    else None
                ),
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
        # Where the action tag changed, so the line can be drawn in its colours.
        result["states"] = [
            {"time": str(change["time"]), "tone": str(change["tone"])}
            for change in (data.get("states") or [])
            if change.get("time") and change.get("tone")
        ]
        result["chart_period"] = {
            "start": result["series"][0]["time"] if result["series"] else None,
            "end": result["series"][-1]["time"] if result["series"] else None,
        }
        # The honest dashed stretch from the last saved bar to the clock.
        result["gap"] = data.get("gap")
        result["note"] = "Price history"
        if market == "stocks":
            result["chart_url"] = f"/api/screens/stocks/{identifier}/chart"
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
