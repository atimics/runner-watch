"""Value the saved portfolios of entities directly linked to one stock."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from runner_web.db import connection
from runner_web.entity_view import entity_view
from runner_web.quotes import _positive_price, _stamp
from runner_web.stock_map import FORMS, filing_events


def _saved_prices(database: Any, tickers: list[str], *, at: datetime | None = None) -> list[dict]:
    """Read both saved quote lanes in batches, with the same mark rules as Calls."""
    prices: dict[str, dict] = {}
    now = at or datetime.now(UTC)
    known_at = now.isoformat()
    for start in range(0, len(tickers), 200):
        batch = tickers[start : start + 200]
        placeholders = ",".join("?" for _ in batch)
        quotes = database.execute(
            f"SELECT ticker,price,observed_at FROM ticker_quotes "
            f"WHERE ticker IN ({placeholders}) AND status='ok' "
            "AND observed_at<=? AND requested_at<=?",
            (*batch, known_at, known_at),
        ).fetchall()
        scans = database.execute(
            "SELECT ticker,price,quote_time AS observed_at FROM ("
            "SELECT ticker,price,quote_time,ROW_NUMBER() OVER "
            "(PARTITION BY ticker ORDER BY captured_at DESC,id DESC) AS rank "
            f"FROM scan_snapshots WHERE ticker IN ({placeholders}) "
            "AND captured_at<=? AND quote_time<=?) ranked WHERE rank=1",
            (*batch, known_at, known_at),
        ).fetchall()
        for row in [*quotes, *scans]:
            price, stamp = _positive_price(row["price"]), _stamp(row["observed_at"])
            if price is None or stamp is None or stamp > now:
                continue
            ticker = row["ticker"]
            if ticker not in prices or stamp.isoformat() > prices[ticker]["observed_at"]:
                prices[ticker] = {
                    "ticker": ticker,
                    "price": price,
                    "observed_at": stamp.isoformat(),
                }
    return list(prices.values())


def cluster_worth(ticker: str) -> dict:
    """Stock -> exact reporting identities -> all of those identities' saved stocks.

    Read the full saved filing set, independently of map or event pagination.
    Candidate matching is batched; parsed identities decide membership.
    """
    return cluster_worths([ticker])[ticker]


def cluster_worths(tickers: list[str], *, at: datetime | None = None) -> dict[str, dict]:
    """Value a candidate set with one shared filing and saved-price read."""
    symbols = list(dict.fromkeys(tickers))
    if not symbols:
        return {}
    known_at = (at or datetime.now(UTC)).isoformat()
    forms = ",".join("?" for _ in FORMS)
    roots_by_symbol: dict[str, list[dict]] = defaultdict(list)
    with connection() as database:
        for start in range(0, len(symbols), 200):
            batch = symbols[start : start + 200]
            placeholders = ",".join("?" for _ in batch)
            roots = database.execute(
                f"SELECT * FROM sec_filings WHERE ticker IN ({placeholders}) "
                f"AND form IN ({forms}) AND filed_at<=? AND created_at<=? AND updated_at<=?",
                (*batch, *FORMS, known_at, known_at, known_at),
            ).fetchall()
            for row in roots:
                roots_by_symbol[row["ticker"]].extend(filing_events(dict(row)))
        people_by_symbol: dict[str, dict[str, dict]] = {}
        all_people: set[str] = set()
        all_events: dict[str, dict] = {}
        for symbol in symbols:
            people = {}
            for event in roots_by_symbol[symbol]:
                all_events[event["id"]] = event
                for person in event["people"]:
                    people.setdefault(person["id"], person)
            people_by_symbol[symbol] = people
            all_people.update(people)
        ciks = sorted(int(identity[4:]) for identity in all_people if identity.startswith("sec:"))
        for start in range(0, len(ciks), 50):
            batch = ciks[start : start + 50]
            placeholders = ",".join("?" for _ in batch)
            matches = " OR ".join("evidence_json LIKE ?" for _ in batch)
            candidates = database.execute(
                f"SELECT * FROM sec_filings WHERE form IN ({forms}) "
                f"AND filed_at<=? AND created_at<=? AND updated_at<=? "
                f"AND (actor_cik IN ({placeholders}) OR {matches})",
                (*FORMS, known_at, known_at, known_at, *batch, *(f"%{cik}%" for cik in batch)),
            ).fetchall()
            for row in candidates:
                for event in filing_events(dict(row)):
                    if any(person["id"] in all_people for person in event["people"]):
                        all_events[event["id"]] = event
        price_symbols = sorted({event["ticker"] for event in all_events.values()})
        prices = _saved_prices(database, price_symbols, at=at)
    events_by_person: dict[str, list[dict]] = defaultdict(list)
    for event in all_events.values():
        for person in event["people"]:
            if person["id"] in all_people:
                events_by_person[person["id"]].append(event)
    result = {}
    for symbol, people in people_by_symbol.items():
        events = {event["id"]: event for identity in people for event in events_by_person[identity]}
        result[symbol] = cluster_summary(
            symbol, list(people.values()), list(events.values()), prices
        )
    return result


def cluster_summary(
    ticker: str, people: list[dict], events: list[dict], prices: list[dict]
) -> dict:
    people_by_id = {person["id"]: person for person in people}
    by_person: dict[str, dict[str, dict]] = defaultdict(dict)
    for event in events:
        for person in event["people"]:
            if person["id"] in people_by_id:
                by_person[person["id"]][event["id"]] = event
    members = []
    stocks: dict[str, dict] = {}
    for identity, person in people_by_id.items():
        portfolio = entity_view(list(by_person[identity].values()), prices, identity)
        holdings = []
        for stock in portfolio["stocks"]:
            symbol, value = stock["ticker"], stock["value"]
            holdings.append({"ticker": symbol, "value": value})
            combined = stocks.setdefault(symbol, {"ticker": symbol, "values": [], "tracked": 0})
            combined["tracked"] += 1
            if value is not None:
                combined["values"].append(value)
        members.append(
            {
                "id": identity,
                "name": person["name"],
                "value": portfolio["value"],
                "covered": portfolio["covered"],
                "tracked": portfolio["tracked"],
                "holdings": holdings,
            }
        )
    values = [member["value"] for member in members if member["value"] is not None]
    stamps = [price["observed_at"] for price in prices if price.get("observed_at")]
    return {
        "ticker": ticker,
        "value": sum(values) if values else None,
        "basis": "Tracked stock holdings at latest saved prices",
        "entity_count": len(members),
        "covered": sum(member["covered"] for member in members),
        "tracked": sum(member["tracked"] for member in members),
        "stock_count": len(stocks),
        "prices_from": min(stamps) if stamps else None,
        "prices_to": max(stamps) if stamps else None,
        "members": sorted(
            members,
            key=lambda member: (-(member["value"] or 0), member["name"], member["id"]),
        ),
        "stocks": sorted(
            [
                {
                    "ticker": symbol,
                    "value": sum(stock["values"]) if stock["values"] else None,
                    "covered": len(stock["values"]),
                    "tracked": stock["tracked"],
                }
                for symbol, stock in stocks.items()
            ],
            key=lambda stock: (-(stock["value"] or 0), stock["ticker"]),
        ),
    }
