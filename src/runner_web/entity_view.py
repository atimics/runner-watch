"""Entity maps and disclosed holdings valued at saved stock prices."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def entity_view(events: list[dict], items: list[dict], person_id: str) -> dict:
    prices = {item["ticker"]: _number(item.get("price")) for item in items}
    groups: dict[str, list[dict]] = defaultdict(list)
    filings: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for event in events:
        if not any(person["id"] == person_id for person in event["people"]):
            continue
        groups[event["ticker"]].append(event)
        if event.get("filed_at") and event.get("security_type") != "derivative":
            filings[(event["filed_at"], event["accession"], event["ticker"])].append(event)
    latest: dict[str, float | None] = {}
    series: dict[str, dict] = {}
    for (filed_at, _, ticker), rows in sorted(filings.items()):
        positions = {}
        complete = True
        securities = set()
        for event in sorted(
            rows,
            key=lambda row: (
                row.get("occurred_at") or "",
                int(re.findall(r"\d+$", row["id"])[0]) if re.findall(r"\d+$", row["id"]) else 0,
            ),
        ):
            security = str(event.get("security") or "").strip().casefold()
            # One quoted share class per ticker; derivatives and ambiguous classes stay unvalued.
            plain = security in {
                "common stock",
                "common shares",
                "ordinary shares",
                "shares",
                "units",
            }
            quantity = _number(
                event.get("shares") if event["view"] == "ownership" else event.get("post_shares")
            )
            if not plain or quantity is None or event.get("joint"):
                complete = False
                continue
            securities.add(security)
            key = (security, event.get("ownership") or "", event.get("ownership_detail") or "")
            positions[key] = quantity
        price = prices.get(ticker)
        latest[ticker] = (
            _number(sum(positions.values()) * price)
            if complete and len(securities) == 1 and positions and price is not None
            else None
        )
        valued = [value for value in latest.values() if value is not None]
        series[filed_at] = {
            "time": filed_at,
            "value": sum(valued) if valued else None,
            "covered": len(valued),
            "tracked": len(latest),
        }
    values = [value for value in latest.values() if value is not None]
    return {
        "stocks": [
            {"ticker": ticker, "events": rows, "value": latest.get(ticker)}
            for ticker, rows in sorted(groups.items())
        ],
        "series": list(series.values()),
        "value": sum(values) if values else None,
        "covered": len(values),
        "tracked": len(groups),
        "basis": "Reported holdings at latest saved prices",
    }
