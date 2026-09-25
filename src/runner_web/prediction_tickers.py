"""One event ticker, explicit outcome contracts, and independent probability histories."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

SOURCES = {
    "rati": "RATi",
    "kalshi": "Kalshi",
    "polymarket": "Polymarket",
    "sportsbook": "Sportsbook",
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def _stamp(value: Any) -> str:
    parsed = _time(value)
    return parsed.strftime("%b %d · %H:%M UTC") if parsed else "Time pending"


def _point(source: str, probability: Any, time: Any, **extra: Any) -> dict[str, Any] | None:
    probability = _number(probability)
    parsed = _time(time)
    if probability is None or not 0 <= probability <= 1 or parsed is None:
        return None
    return {
        "source": source,
        "probability": probability,
        "observed_at": parsed.isoformat(),
        **extra,
    }


def _chart(points: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not points:
        return None
    times = [_time(point["observed_at"]).timestamp() for point in points]
    first, last = min(times), max(times)
    span = last - first
    series = []
    for source, label in SOURCES.items():
        saved = sorted((p for p in points if p["source"] == source), key=lambda p: p["observed_at"])
        if not saved:
            continue
        path, dots, prior = [], [], None
        for point in saved:
            time = _time(point["observed_at"])
            x = 50 if span == 0 else 4 + (time.timestamp() - first) / span * 92
            y = 56 - point["probability"] * 52
            # Leave gaps visible when the collector has missed several intervals.
            command = "M" if prior is None or time - prior > timedelta(hours=2) else "L"
            path.append(f"{command}{x:.3f},{y:.3f}")
            dots.append([round(x, 3), round(y, 3)])
            prior = time
        series.append(
            {
                "key": source,
                "label": label,
                "path": " ".join(path),
                "dots": dots,
                "count": len(saved),
                "last": round(saved[-1]["probability"] * 100, 1),
                "time": _stamp(saved[-1]["observed_at"]),
                "move": round((saved[-1]["probability"] - saved[0]["probability"]) * 100, 1),
            }
        )
    return {
        "series": series,
        "first": _stamp(datetime.fromtimestamp(first, UTC)),
        "last": _stamp(datetime.fromtimestamp(last, UTC)),
        "count": len(set(times)),
    }


def _team_contract(event: dict[str, Any]) -> dict[str, Any]:
    prediction = event.get("prediction") or {}
    history = event.get("prediction_history") or ([prediction] if prediction else [])
    start = _time(event.get("start_time"))
    contract: dict[str, Any] = {
        "key": "winner",
        "label": "Game winner",
        "unit": "Win chance",
        "rules": "Full game, including overtime, conditional on a decisive result. "
        "Each venue pair is scaled to 100%. Venue rules govern ties and cancellations.",
        "model_label": "Pregame baseline",
        "model_version": prediction.get("model_version", "team-form-v1"),
        "method": "Season wins and losses, smoothed with an 8–8 prior, plus home advantage. "
        "The record difference is multiplied by 0.65. Chances stay between 18% and 82%.",
        "model_note": "Experimental baseline · calibration under review",
        "record": event.get("model_record"),
        "outcomes": [],
    }
    for side in ("away", "home"):
        points = []
        for model in history:
            at = _time(model.get("observed_at"))
            if (
                at is None
                or (start and at >= start)
                or model.get("model_version") != prediction.get("model_version")
            ):
                continue
            for source, key in [
                ("rati", f"{side}_probability"),
                ("sportsbook", f"{side}_market_probability"),
            ]:
                point = _point(
                    source,
                    model.get(key),
                    at,
                    basis="Saved no-vig moneyline"
                    if source == "sportsbook"
                    else "Season record + venue",
                )
                if point:
                    points.append(point)
        histories = event.get("prediction_market_history") or {}
        for source in ("kalshi", "polymarket"):
            quotes = histories.get(source) or [
                q for q in event.get("prediction_markets") or [] if q.get("source") == source
            ]
            for quote in quotes:
                at = _time(quote.get("observed_at"))
                if at is None or (start and at >= start):
                    continue
                point = _point(
                    source,
                    quote.get(f"{side}_probability"),
                    at,
                    basis=quote.get("price_basis"),
                    url=quote.get("source_url"),
                    quality=quote.get("quality", "quoted"),
                )
                if point:
                    points.append(point)
        contract["outcomes"].append(
            {
                "key": side,
                "label": str(event.get(f"{side}_abbreviation") or side.title()),
                "name": str(event.get(f"{side}_team_name") or side.title()),
                "points": points,
                "model": _number(prediction.get(f"{side}_probability")),
                "model_at": prediction.get("observed_at"),
            }
        )
    return contract


def _cup_contracts(event: dict[str, Any]) -> list[dict[str, Any]]:
    analysis = event.get("analysis") or {}
    prediction = analysis.get("prediction") or {}
    history = event.get("analysis_history") or ([analysis] if analysis else [])
    contracts = []
    for key, label in [("winner", "Cup winner"), ("winner-half-tie", "Winner · half on a tie")]:
        half_tie = key == "winner-half-tie"
        contract: dict[str, Any] = {
            "key": key,
            "label": label,
            "unit": "Fair value" if half_tie else "Win chance",
            "model_label": "Completed-match baseline",
            "model_version": analysis.get("model_version", "cup-ranking-v1"),
            "rules": (
                "A win pays $1; a 15–15 tie pays 50¢. "
                "RATi fair value = win chance + half the tie chance."
                if half_tie
                else "USA or International must reach 15.5 points. "
                "A 15–15 finish settles the Tie outcome."
            ),
            "method": "Player world ranking points set match strength. "
            "Announced pairings use their named players. "
            "Future pairings use roster averages. Each remaining match has a 12% tie assumption. "
            "The model combines all remaining match outcomes with the points already earned.",
            "model_note": "Experimental baseline · 12% match tie assumption · "
            "updates after completed matches",
            "outcomes": [],
            "stale": bool(analysis.get("stale") or analysis.get("refresh_pending")),
        }
        for outcome, name in [("usa", "USA"), ("international", "International"), ("tie", "Tie")]:
            if half_tie and outcome == "tie":
                continue
            model = _number(prediction.get(outcome))
            if model is not None and half_tie:
                model += float(prediction.get("tie") or 0) / 2
            points = []
            for snapshot in history:
                pred = snapshot.get("prediction") or {}
                chance = _number(pred.get(outcome))
                if chance is None or snapshot.get("model_version") != analysis.get("model_version"):
                    continue
                if half_tie:
                    chance += float(pred.get("tie") or 0) / 2
                point = _point("rati", chance, snapshot.get("captured_at"))
                if point:
                    points.append(point)
            points.extend(
                q
                for q in event.get("contract_quotes") or []
                if q.get("contract_key") == key and q.get("outcome_key") == outcome
            )
            contract["outcomes"].append(
                {
                    "key": outcome,
                    "label": name,
                    "name": name,
                    "points": points,
                    "model": model,
                    "model_at": analysis.get("captured_at"),
                }
            )
        contracts.append(contract)
    return contracts


def ticker(
    event: dict[str, Any], *, outcome: str = "", contract: str = "", now: datetime | None = None
) -> dict[str, Any]:
    """Build the same outcome view for team games and multi-outcome events."""
    current = now or datetime.now(UTC)
    golf = str(event.get("id", "")).startswith("golf:")
    cup = event.get("id") == "golf:401824815"
    contracts = _cup_contracts(event) if cup else [] if golf else [_team_contract(event)]
    selected_contract = next(
        (c for c in contracts if c["key"] == contract), next(iter(contracts), None)
    )
    identifier = str(event.get("id") or "").upper()
    result: dict[str, Any] = {
        "symbol": identifier,
        "contracts": contracts,
        "contract": selected_contract,
        "selected": None,
        "score_call": None,
        "pending": "Forecast needs a full field, player scoring history, and course conditions.",
    }
    if cup:
        prediction = (event.get("analysis") or {}).get("prediction") or {}
        if prediction:
            result["score_call"] = {
                "label": "Expected final points",
                "value": f"USA {prediction['expected_usa']:.1f} – "
                f"{prediction['expected_international']:.1f} International",
            }
        result["pending"] = "Forecast needs the official rosters, rankings, and match scores."
    elif not golf and event.get("prediction") and event.get("projected_home_score_display"):
        result["score_call"] = {
            "label": "Score estimate · " + str(event.get("projected_score_basis") or "baseline"),
            "value": f"{event.get('away_abbreviation')} {event['projected_away_score_display']} – "
            f"{event['projected_home_score_display']} {event.get('home_abbreviation')}",
        }
    if selected_contract is None:
        return result
    reference = min(current, _time(event.get("start_time")) or current) if not cup else current
    for c in contracts:
        c["href"] = "?" + urlencode({"contract": c["key"]})
        for o in c["outcomes"]:
            o["href"] = "?" + urlencode({"contract": c["key"], "outcome": o["key"]})
            valid = [
                p
                for p in o["points"]
                if _time(p.get("observed_at")) and _number(p.get("probability")) is not None
            ]
            o["points"] = valid
            latest = {}
            for p in sorted(valid, key=lambda p: p["observed_at"]):
                latest[p["source"]] = p
            venues = []
            model_at = _time(o.get("model_at"))
            for source in ("kalshi", "polymarket", "sportsbook"):
                if source not in latest:
                    continue
                quote = latest[source]
                at = _time(quote["observed_at"])
                stale = reference - at > timedelta(minutes=30)
                model_stale = (
                    c.get("stale")
                    or model_at is None
                    or reference - model_at > timedelta(minutes=30)
                )
                quality = quote.get("quality", "quoted")
                gap = (
                    (o["model"] - quote["probability"]) * 100
                    if o["model"] is not None
                    and not stale
                    and not model_stale
                    and quality == "quoted"
                    else None
                )
                venues.append(
                    {
                        **quote,
                        "key": source,
                        "label": SOURCES[source],
                        "percent": round(quote["probability"] * 100, 1),
                        "gap": round(gap, 1) if gap is not None else None,
                        "saved_label": _stamp(quote["observed_at"]),
                        "stale": stale,
                        "status": "Earlier price"
                        if stale
                        else "Wide spread"
                        if quality != "quoted"
                        else "Quoted",
                        "spread_pct": round(quote["spread"] * 100, 1)
                        if quote.get("spread") is not None
                        else None,
                    }
                )
            o["venues"] = venues
            o["benchmark"] = next(
                (v for v in venues if v["gap"] is not None), next(iter(venues), None)
            )
            o["percent"] = round(o["model"] * 100, 1) if o["model"] is not None else None
            o["model_label"] = _stamp(o.get("model_at"))
            o["chart"] = _chart(valid)
            o["gap"] = o["benchmark"]["gap"] if o["benchmark"] else None
    outcomes = selected_contract["outcomes"]
    favorite = max(outcomes, key=lambda o: o["model"] if o["model"] is not None else -1)
    result["selected"] = next((o for o in outcomes if o["key"] == outcome), favorite)
    selected = result["selected"]
    benchmark = selected["benchmark"]
    result["description"] = (
        f"{selected['label']}: RATi {selected['percent']}%"
        if selected["percent"] is not None
        else f"{selected['label']}: model pending"
    )
    if benchmark:
        result["description"] += f", {benchmark['label']} {benchmark['percent']}%"
    if selected["gap"] is not None:
        result["description"] += f", gap {selected['gap']:+.1f} percentage points"
    return result
