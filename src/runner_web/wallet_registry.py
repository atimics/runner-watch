"""Canonical, hashed wallet ids over the participant identity layer.

A wallet used to be addressed by the stock it was read from -- ``/wallets/stocks/
AAPL/sec:1475597`` -- even though a SEC CIK identity is global, and each new kind
(SEC, DiD, Solana) would have invented another shape. Here every wallet gets one
stable id, minted as a hash of its kind, value and scope, and registered in the
identity tables that already exist for exactly this: a ``participant_entities``
row keyed by the wallet id, with a ``participant_references`` row describing what
the wallet is (a SEC filer id, a reported name scoped to a ticker, a chain
address, a DiD).

The hash hides the underlying identifier, gives every kind the same URL shape,
and keeps identities mergeable: two wallets that turn out to be one participant
can be joined with a ``same_participant`` claim without changing either id.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

from runner_web.database import DatabaseConnection
from runner_web.db import connection
from runner_web.identity import attach_reference, ensure_entity

WALLET_PREFIX = "w_"
SEC_PERSON = re.compile(r"sec:[1-9][0-9]{0,9}")
NAME_PERSON = re.compile(r"name:[0-9a-f]{20}")
WALLET_ID = re.compile(r"w_[0-9a-f]{16}")

# Reference kinds we mint: SEC filer ids and reported names both live in the
# filing domain, chain addresses and DiDs are wallets in the identity layer.
SEC_ISSUER = "sec"
DID_ISSUER = "did"


def wallet_id_for(*, kind: str, value: str, scope: str = "") -> str:
    """One id per (kind, value, scope). Stable, opaque and URL-safe."""

    digest = hashlib.sha256(
        f"{kind.casefold()}|{value.strip()}|{scope.strip().upper()}".encode()
    ).hexdigest()[:16]
    return f"{WALLET_PREFIX}{digest}"


def wallet_id_for_person(person_id: str, ticker: str | None = None) -> str | None:
    """Bridge the older ``sec:``/``name:`` ids onto wallet ids."""

    if SEC_PERSON.fullmatch(person_id):
        return wallet_id_for(kind="sec", value=person_id.removeprefix("sec:"))
    if NAME_PERSON.fullmatch(person_id):
        # A reported name only means anything within the ticker it was read from.
        return wallet_id_for(
            kind="name", value=person_id.removeprefix("name:"), scope=ticker or ""
        )
    return None


def person_parts(person_id: str, ticker: str | None = None) -> dict[str, Any] | None:
    """The stored shape of a person identity: what it is and what it is scoped to."""

    wallet = wallet_id_for_person(person_id, ticker)
    if wallet is None:
        return None
    if SEC_PERSON.fullmatch(person_id):
        return {
            "wallet_id": wallet,
            "person_id": person_id,
            "reference_kind": "filing",
            "value": person_id.removeprefix("sec:"),
            "issuing_system": SEC_ISSUER,
            "scope": "",
            "entity_kind": "organization",
        }
    return {
        "wallet_id": wallet,
        "person_id": person_id,
        "reference_kind": "name",
        "value": person_id.removeprefix("name:"),
        "issuing_system": None,
        "scope": (ticker or "").upper(),
        "entity_kind": "person",
    }


def register_person(
    person_id: str,
    ticker: str | None = None,
    *,
    database: DatabaseConnection | None = None,
) -> str | None:
    """Make a person addressable as a wallet and return its id."""

    parts = person_parts(person_id, ticker)
    if parts is None:
        return None
    moment = datetime.now(UTC).isoformat()
    if database is not None:
        _register(database, parts, moment)
        return str(parts["wallet_id"])
    with connection() as db:
        _register(db, parts, moment)
    return str(parts["wallet_id"])


def _register(database: DatabaseConnection, parts: dict[str, Any], moment: str) -> None:
    entity = ensure_entity(
        str(parts["entity_kind"]),
        dedupe_key=str(parts["wallet_id"]),
        created_at=moment,
        connection=database,
    )
    attach_reference(
        str(entity["id"]),
        str(parts["reference_kind"]),
        str(parts["value"]),
        issuing_system=parts["issuing_system"],
        learned_at=moment,
        source_kind="ticker" if parts["scope"] else "sec",
        source_id=parts["scope"] or None,
        connection=database,
    )


def register_people(
    events: list[dict[str, Any]],
    ticker: str,
    *,
    database: DatabaseConnection | None = None,
) -> int:
    """Register every person named by a page of filings, once each."""

    seen: dict[str, str | None] = {}
    for event in events:
        for person in event.get("people") or []:
            identity = str(person.get("id") or "")
            if identity and identity not in seen:
                seen[identity] = None
    if not seen:
        return 0
    moments = datetime.now(UTC).isoformat()
    parts = [
        part
        for part in (person_parts(identity, ticker) for identity in seen)
        if part is not None
    ]
    if not parts:
        return 0
    if database is not None:
        for part in parts:
            _register(database, part, moments)
        return len(parts)
    with connection() as db:
        for part in parts:
            _register(db, part, moments)
    return len(parts)


def wallet(wallet_id: str, *, database: DatabaseConnection | None = None) -> dict[str, Any] | None:
    """Resolve a wallet id back to what it identifies, or None when unknown."""

    if not WALLET_ID.fullmatch(wallet_id):
        return None
    if database is not None:
        return _wallet(database, wallet_id)
    with connection() as db:
        return _wallet(db, wallet_id)


def _wallet(database: DatabaseConnection, wallet_id: str) -> dict[str, Any] | None:
    entity = database.execute(
        "SELECT id,kind,status FROM participant_entities WHERE dedupe_key=?", (wallet_id,)
    ).fetchone()
    if not entity:
        return None
    reference = database.execute(
        """
        SELECT kind,value,issuing_system,source_id FROM participant_references
        WHERE entity_id=? AND kind IN ('filing','name','wallet')
        ORDER BY learned_at LIMIT 1
        """,
        (entity["id"],),
    ).fetchone()
    if not reference:
        return None
    kind = str(reference["kind"])
    if kind == "filing" or str(reference["issuing_system"] or "") == SEC_ISSUER:
        return {
            "id": wallet_id,
            "entity_id": str(entity["id"]),
            "kind": "sec",
            "person_id": f"sec:{reference['value']}",
            "name": None,
            "scope": "",
        }
    if kind == "name":
        return {
            "id": wallet_id,
            "entity_id": str(entity["id"]),
            "kind": "name",
            "person_id": f"name:{reference['value']}",
            "name": None,
            "scope": str(reference["source_id"] or ""),
        }
    return {
        "id": wallet_id,
        "entity_id": str(entity["id"]),
        "kind": str(reference["issuing_system"] or "wallet"),
        "person_id": None,
        "name": str(reference["value"]),
        "scope": "",
    }
