"""Recompute a recorded result from its facts, under this version of the rules.

The RATi service records each change in a result with the rules version, its
digest and the facts the rules read (JSON, so dates are text). With the files
of that version, published on trust.rati.chat once it is replaced, anyone can
run:

    from ratitrust.reproduce import reproduce
    reproduce("stock", record["facts"]) == record["result"]

Only the version that recorded a result reproduces it exactly.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ratitrust import memecoin, stock


def _date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def reproduce(market: str, facts: dict[str, Any]) -> dict[str, Any]:
    """The result the rules give for recorded `facts` ("stock" or "memecoin")."""

    if market == "stock":
        filed = facts.get("filed")
        return stock.standards(
            exchange=facts.get("exchange"),
            issuer=facts.get("issuer") or {},
            halted_on=_date(facts.get("halted_on")),
            delisting_on=_date(facts.get("delisting_on")),
            today=_date(facts["today"]),
            filed={**filed, "forms": set(filed.get("forms") or ())} if filed else None,
            delistings_read=bool(facts.get("delistings_read", True)),
        )
    if market == "memecoin":
        return memecoin.standards(
            facts.get("row") or {},
            facts.get("controls"),
            facts.get("top10_pct"),
            bool(facts.get("bundled")),
            datetime.fromisoformat(str(facts["at"])),
            holder_count=facts.get("holder_count"),
            holder_count_at_least=bool(facts.get("holder_count_at_least")),
            lock=facts.get("lock"),
        )
    raise ValueError(f"Unknown market: {market}")
