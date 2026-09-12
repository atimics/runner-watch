"""Pseudonymized market actors: stock insiders and on-chain wallet clusters.

An actor is a fictional character with a stable handle and face. It represents a
real SEC filer or an observed wallet cluster, but it never carries the real
name. Every tie to a market is evidence-linked so the public surface can always
point back to the filing or the transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from runner_web.db import connection
from runner_web.pseudonyms import comment_avatar_profile, derive_actor_identity

ACTOR_USER_PREFIX = "actor-"
ACTOR_ID_PREFIX = "ma-"
MAX_MAP_ACTORS = 160
MAX_MAP_LINKS = 4_000
MAX_SUBJECT_ACTORS = 14
DEFAULT_DERIVE_LIMIT = 5_000
CLUSTER_MIN_WALLETS = 3
ACTOR_COMMENT_PER_DAY = max(0, int(os.getenv("ACTOR_COMMENT_PER_ACTOR_DAILY", "3")))
ACTOR_COMMENT_DAILY_LIMIT = max(0, int(os.getenv("ACTOR_COMMENT_DAILY_LIMIT", "300")))
ACTOR_DERIVE_INTERVAL_SECONDS = max(120, int(os.getenv("MARKET_ACTOR_DERIVE_SECONDS", "900")))
STOCK_FORMS = ("4", "SC 13D", "SC 13G")


def _iso(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).isoformat()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _stable_key(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}:{value}".encode()).hexdigest()


def _normalize_name(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def coin_subject_key(token_address: str, network: str = "solana") -> str:
    """The board's coin id for a token, matching normalize_chain_pools."""

    return "chain-" + hashlib.sha256(f"{network}:{token_address}".encode()).hexdigest()


def actor_id_for(stable_key: str) -> str:
    return ACTOR_ID_PREFIX + stable_key[:24]


def _actor_user_id(stable_key: str) -> str:
    return ACTOR_USER_PREFIX + stable_key[:24]


def _ensure_actor(
    db: Any,
    *,
    kind: str,
    domain: str,
    stable_key: str,
) -> dict[str, Any]:
    existing = db.execute(
        "SELECT * FROM market_actors WHERE stable_key=?", (stable_key,)
    ).fetchone()
    if existing:
        return dict(existing)
    identity = derive_actor_identity(stable_key)
    actor_id = actor_id_for(stable_key)
    user_id = _actor_user_id(stable_key)
    name = identity["name"]
    clash = db.execute(
        "SELECT user_id FROM comment_avatars WHERE name=?", (name,)
    ).fetchone()
    if clash is not None and str(clash["user_id"]) != user_id:
        name = f"{name} {stable_key[:4].upper()}"
    timestamp = _iso()
    db.execute(
        """
        INSERT INTO users(id,username,display_name,status,created_at)
        VALUES(?,?,?,'active',?) ON CONFLICT DO NOTHING
        """,
        (user_id, "actor_" + stable_key[:24], name, timestamp),
    )
    db.execute(
        """
        INSERT INTO comment_avatars(user_id,name,seed,ability_id,level,created_at)
        VALUES(?,?,?,?,1,?) ON CONFLICT DO NOTHING
        """,
        (user_id, name, identity["seed"], identity["ability_id"], timestamp),
    )
    db.execute(
        """
        INSERT INTO market_actors(
            id,kind,domain,stable_key,user_id,display_name,ability_id,avatar_seed,
            portrait_status,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,'pending',?,?) ON CONFLICT(stable_key) DO NOTHING
        """,
        (
            actor_id,
            kind,
            domain,
            stable_key,
            user_id,
            name,
            identity["ability_id"],
            identity["seed"],
            timestamp,
            timestamp,
        ),
    )
    row = db.execute(
        "SELECT * FROM market_actors WHERE stable_key=?", (stable_key,)
    ).fetchone()
    return dict(row)


def _insert_tie(
    db: Any,
    actor_id: str,
    *,
    subject_kind: str,
    subject_key: str,
    role: str,
    direction: str,
    weight: float | None,
    as_of: str,
    evidence_kind: str,
    evidence_id: str,
    created_at: str,
) -> bool:
    identity = "|".join(
        (actor_id, subject_kind, subject_key, role, direction, evidence_kind, evidence_id)
    )
    tie_id = "tie-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    result = db.execute(
        """
        INSERT INTO actor_ties(
            id,actor_id,subject_kind,subject_key,role,direction,weight,as_of,
            evidence_kind,evidence_id,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
        """,
        (
            tie_id,
            actor_id,
            subject_kind,
            subject_key,
            role,
            direction,
            weight,
            as_of,
            evidence_kind,
            evidence_id,
            created_at,
        ),
    )
    return bool(result.rowcount)


def _insert_cluster_member(
    db: Any,
    actor_id: str,
    wallet: str,
    evidence_kind: str,
    evidence_id: str,
    created_at: str,
) -> bool:
    result = db.execute(
        """
        INSERT INTO actor_cluster_members(actor_id,wallet,evidence_kind,evidence_id,created_at)
        VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING
        """,
        (actor_id, wallet, evidence_kind, evidence_id, created_at),
    )
    return bool(result.rowcount)


def _stock_role(title: str, person_types: str) -> str:
    text = f"{title} {person_types}".casefold()
    if "director" in text:
        return "director"
    if "10%" in text or "ten percent" in text or "ten_percent" in text:
        return "ten_percent_owner"
    if title.strip():
        return "officer"
    return "insider"


def _stock_owners(row: Any) -> list[tuple[str, str]]:
    owners: dict[str, str] = {}
    actor = " ".join(str(row["actor"] or "").split())
    if actor:
        owners[actor] = _stock_role(
            str(row["actor_title"] or ""), str(row["reporting_person_types"] or "")
        )
    for raw in str(row["beneficial_owner_names"] or "").split(","):
        name = " ".join(raw.split())
        if name:
            owners.setdefault(name, "ten_percent_owner")
    return sorted((name, role) for name, role in owners.items() if name)


def _stock_direction(row: Any) -> str:
    codes = str(row["transaction_codes"] or "").upper()
    if "P" in codes:
        return "buy"
    if "S" in codes:
        return "sell"
    if str(row["form"] or "").startswith("SC 13"):
        return "own"
    return "hold"


def derive_stock_actors(
    database: Any = None,
    *,
    at: datetime | None = None,
    limit: int = DEFAULT_DERIVE_LIMIT,
) -> int:
    """Turn recent Form 4 / SC 13 filings into actors and ties. Idempotent."""

    timestamp = _iso(at)
    connected = _connection(database)
    inserted = 0
    with connected as db:
        rows = db.execute(
            """
            SELECT accession,ticker,form,actor,actor_title,reporting_person_types,
                   beneficial_owner_names,transaction_codes,transaction_value,
                   beneficial_ownership_pct,filed_at
            FROM sec_filings
            WHERE form IN ('4','SC 13D','SC 13G')
            ORDER BY filed_at DESC LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        for row in rows:
            ticker = str(row["ticker"] or "").strip().upper()
            if not ticker:
                continue
            owners = _stock_owners(row)
            if not owners:
                continue
            direction = _stock_direction(row)
            weight = _number(row["transaction_value"])
            if weight is None:
                weight = _number(row["beneficial_ownership_pct"])
            as_of = str(row["filed_at"] or timestamp)
            evidence_id = str(row["accession"] or "")
            for name, role in owners:
                actor = _ensure_actor(
                    db,
                    kind="person",
                    domain="stock",
                    stable_key=_stable_key("insider", _normalize_name(name)),
                )
                if _insert_tie(
                    db,
                    str(actor["id"]),
                    subject_kind="stock",
                    subject_key=ticker,
                    role=role,
                    direction=direction,
                    weight=weight,
                    as_of=as_of,
                    evidence_kind="sec_filing",
                    evidence_id=evidence_id,
                    created_at=timestamp,
                ):
                    inserted += 1
    return inserted


def _finding_wallets(finding: dict[str, Any]) -> list[str]:
    wallets = {str(finding.get("wallet") or "").strip()}
    for value in finding.get("related_wallets") or []:
        wallets.add(str(value).strip())
    return sorted(wallet for wallet in wallets if wallet)


def _finding_direction(kind: str) -> str:
    lowered = kind.casefold()
    if "sell" in lowered or "withdraw" in lowered:
        return "sell"
    if "buy" in lowered or "transfer" in lowered:
        return "buy"
    return "hold"


def derive_coin_actors(
    database: Any = None,
    *,
    at: datetime | None = None,
    limit: int = 400,
) -> int:
    """Turn saved wallet-cluster findings into actors and ties. Idempotent."""

    timestamp = _iso(at)
    connected = _connection(database)
    inserted = 0
    with connected as db:
        row = db.execute(
            "SELECT value FROM worker_state WHERE key='memecoin_forensics'"
        ).fetchone()
        if row is None:
            return 0
        try:
            forensics = json.loads(row["value"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0
        for finding in list(forensics.get("findings") or [])[: max(1, int(limit))]:
            token = str(finding.get("token_address") or "").strip()
            kind = str(finding.get("kind") or "")
            wallets = _finding_wallets(finding)
            if not token or not wallets:
                continue
            is_creator = kind.startswith("creator")
            if len(wallets) < CLUSTER_MIN_WALLETS and not is_creator:
                continue
            cluster_key = _stable_key(
                "cluster", kind.split("_")[0] + "|" + "|".join(wallets)
            )
            actor = _ensure_actor(db, kind="cluster", domain="coin", stable_key=cluster_key)
            actor_id = str(actor["id"])
            role = "creator" if is_creator else "funder"
            direction = _finding_direction(kind)
            evidence_id = str(finding.get("signature") or "")
            as_of = str(finding.get("observed_at") or timestamp)
            for wallet in wallets:
                _insert_cluster_member(
                    db, actor_id, wallet, "chain_event", evidence_id, timestamp
                )
            if _insert_tie(
                db,
                actor_id,
                subject_kind="coin",
                subject_key=coin_subject_key(token),
                role=role,
                direction=direction,
                weight=None,
                as_of=as_of,
                evidence_kind="chain_event",
                evidence_id=evidence_id,
                created_at=timestamp,
            ):
                inserted += 1
    return inserted


def _connection(database: Any) -> Any:
    if database is not None:
        return _ExistingConnection(database)
    return connection()


class _ExistingConnection:
    """Adapt an already-open database for the ``with`` shape used here."""

    def __init__(self, database: Any) -> None:
        self._database = database

    def __enter__(self) -> Any:
        return self._database

    def __exit__(self, *exc: Any) -> bool:
        return False


def _coin_labels(db: Any) -> dict[str, str]:
    row = db.execute(
        "SELECT value FROM worker_state WHERE key='memecoins_snapshot'"
    ).fetchone()
    labels: dict[str, str] = {}
    if row is None:
        return labels
    try:
        snapshot = json.loads(row["value"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return labels
    for coin in snapshot.get("rows") or []:
        coin_id = str(coin.get("id") or "")
        if coin_id:
            labels[coin_id] = str(coin.get("symbol") or "")
    return labels


def _subject_payload(
    subject_kind: str, subject_key: str, labels: dict[str, str]
) -> dict[str, Any]:
    if subject_kind == "stock":
        return {
            "key": subject_key,
            "label": subject_key,
            "kind": "stock",
            "url": f"/t/{subject_key}",
        }
    return {
        "key": subject_key,
        "label": labels.get(subject_key) or subject_key[-6:],
        "kind": "coin",
        "url": f"/memecoins/coin/{subject_key}",
    }


def market_actor_map(domain: str, *, at: datetime | None = None) -> dict[str, Any]:
    domain = "coin" if str(domain).casefold() == "coin" else "stock"
    with connection() as db:
        labels = _coin_labels(db)
        rows = db.execute(
            """
            SELECT t.actor_id,t.subject_kind,t.subject_key,t.role,t.direction,
                   t.weight,t.as_of,t.evidence_kind,t.evidence_id,
                   a.kind,a.display_name,a.ability_id,a.avatar_seed,a.portrait_status
            FROM actor_ties t
            JOIN market_actors a ON a.id=t.actor_id
            WHERE a.domain=?
            ORDER BY t.as_of DESC
            LIMIT ?
            """,
            (domain, MAX_MAP_LINKS),
        ).fetchall()
    actors: dict[str, dict[str, Any]] = {}
    subjects: dict[str, dict[str, Any]] = {}
    links: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        actor_id = str(row["actor_id"])
        subject_kind = str(row["subject_kind"])
        subject_key = str(row["subject_key"])
        weight = _number(row["weight"]) or 0.0
        actor = actors.setdefault(
            actor_id,
            {
                "id": actor_id,
                "name": str(row["display_name"]),
                "kind": str(row["kind"]),
                "domain": domain,
                "ability_id": str(row["ability_id"]),
                "avatar_seed": str(row["avatar_seed"]),
                "portrait_ready": str(row["portrait_status"]) == "ready",
                "portrait_url": f"/api/market-actors/{actor_id}/portrait",
                "roles": set(),
                "subjects": set(),
                "weight": 0.0,
                "last_seen": "",
            },
        )
        actor["roles"].add(str(row["role"]))
        actor["subjects"].add(subject_key)
        actor["weight"] += weight
        actor["last_seen"] = max(actor["last_seen"], str(row["as_of"]))
        subject = subjects.setdefault(
            subject_key,
            {
                "key": subject_key,
                "actor_ids": set(),
                "weight": 0.0,
                "last_seen": "",
                **_subject_payload(subject_kind, subject_key, labels),
            },
        )
        subject["actor_ids"].add(actor_id)
        subject["weight"] += weight
        subject["last_seen"] = max(subject["last_seen"], str(row["as_of"]))
        key = (actor_id, subject_key)
        link = links.setdefault(
            key,
            {
                "actor_id": actor_id,
                "subject_key": subject_key,
                "direction": str(row["direction"]),
                "weight": 0.0,
                "as_of": "",
                "evidence_kind": str(row["evidence_kind"]),
                "evidence_id": str(row["evidence_id"]),
            },
        )
        link["weight"] += weight
        link["as_of"] = max(link["as_of"], str(row["as_of"]))
    ranked = sorted(
        actors.values(), key=lambda item: (item["last_seen"], item["weight"]), reverse=True
    )[:MAX_MAP_ACTORS]
    keep = {item["id"] for item in ranked}
    ordered_subjects = []
    for subject in subjects.values():
        member_ids = [actor_id for actor_id in subject["actor_ids"] if actor_id in keep]
        if not member_ids:
            continue
        ordered_subjects.append(
            {
                "key": subject["key"],
                "label": subject["label"],
                "kind": subject["kind"],
                "url": subject["url"],
                "actor_count": len(member_ids),
                "actor_ids": sorted(
                    member_ids,
                    key=lambda actor_id: actors[actor_id]["last_seen"],
                    reverse=True,
                )[:MAX_SUBJECT_ACTORS],
                "weight": subject["weight"],
                "last_seen": subject["last_seen"],
            }
        )
    ordered_subjects.sort(key=lambda item: (item["last_seen"], item["actor_count"]), reverse=True)
    actor_payloads = [
        {
            **{key: value for key, value in actor.items() if key not in {"roles", "subjects"}},
            "roles": sorted(actor["roles"]),
            "subjects": sorted(actor["subjects"]),
        }
        for actor in ranked
    ]
    link_payloads = [
        link
        for link in links.values()
        if link["actor_id"] in keep
    ]
    return {
        "domain": domain,
        "generated_at": _iso(at),
        "actors": actor_payloads,
        "subjects": ordered_subjects,
        "links": link_payloads,
        "counts": {
            "actors": len(actor_payloads),
            "subjects": len(ordered_subjects),
            "links": len(link_payloads),
        },
    }


def _actor_evidence(db: Any, actor: dict[str, Any]) -> list[dict[str, Any]]:
    ties = db.execute(
        """
        SELECT subject_kind,subject_key,role,direction,weight,as_of,evidence_kind,evidence_id
        FROM actor_ties WHERE actor_id=? ORDER BY as_of DESC LIMIT 25
        """,
        (str(actor["id"]),),
    ).fetchall()
    evidence: list[dict[str, Any]] = []
    for tie in ties:
        entry = {
            "subject_kind": str(tie["subject_kind"]),
            "subject_key": str(tie["subject_key"]),
            "role": str(tie["role"]),
            "direction": str(tie["direction"]),
            "weight": _number(tie["weight"]),
            "as_of": str(tie["as_of"]),
            "evidence_kind": str(tie["evidence_kind"]),
            "evidence_id": str(tie["evidence_id"]),
            "url": "",
            "title": "",
        }
        if entry["evidence_kind"] == "sec_filing":
            filing = db.execute(
                """
                SELECT form,actor_title,filed_at,filing_url,transaction_value,
                       beneficial_ownership_pct
                FROM sec_filings WHERE accession=?
                """,
                (entry["evidence_id"],),
            ).fetchone()
            if filing is not None:
                entry["url"] = str(filing["filing_url"] or "")
                entry["title"] = (
                    f"{filing['form']} · {filing['actor_title'] or entry['role']}"
                )
                if entry["weight"] is None:
                    entry["weight"] = _number(filing["transaction_value"]) or _number(
                        filing["beneficial_ownership_pct"]
                    )
        elif entry["evidence_kind"] == "chain_event":
            entry["url"] = f"/api/memecoins/evidence/{entry['evidence_id']}"
            entry["title"] = "On-chain transaction"
        evidence.append(entry)
    return evidence


def _actor_comments(db: Any, actor_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT id,subject_key,body,generation_model,created_at
        FROM market_actor_comments
        WHERE actor_id=?
        ORDER BY created_at DESC,id DESC LIMIT ?
        """,
        (actor_id, max(1, min(int(limit), 50))),
    ).fetchall()
    return [
        {
            "id": str(row["id"]),
            "subject_key": str(row["subject_key"]),
            "body": str(row["body"]),
            "created_at": str(row["created_at"]),
            "generation_model": str(row["generation_model"] or ""),
        }
        for row in rows
    ]


def market_actor_detail(actor_id: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute("SELECT * FROM market_actors WHERE id=?", (actor_id,)).fetchone()
        if row is None:
            return None
        actor = dict(row)
        evidence = _actor_evidence(db, actor)
        comments = _actor_comments(db, actor_id)
        members = [
            str(member["wallet"])
            for member in db.execute(
                "SELECT wallet FROM actor_cluster_members WHERE actor_id=? ORDER BY wallet",
                (actor_id,),
            ).fetchall()
        ]
    profile = comment_avatar_profile(
        str(actor["display_name"]),
        str(actor["avatar_seed"]),
        str(actor["ability_id"]),
    )
    subjects: dict[str, dict[str, Any]] = {}
    for entry in evidence:
        subjects.setdefault(entry["subject_key"], entry)
    return {
        "actor": {
            "id": actor_id,
            "name": str(actor["display_name"]),
            "user_id": str(actor["user_id"]),
            "kind": str(actor["kind"]),
            "domain": str(actor["domain"]),
            "ability_id": str(actor["ability_id"]),
            "avatar_seed": str(actor["avatar_seed"]),
            "avatar": profile,
            "portrait_ready": str(actor["portrait_status"]) == "ready",
            "portrait_url": f"/api/market-actors/{actor_id}/portrait",
            "roles": sorted({entry["role"] for entry in evidence}),
            "subjects": sorted(subjects),
            "wallets": members,
        },
        "evidence": evidence,
        "comments": [
            {**comment, "alias": profile["name"], "avatar": profile, "author_kind": "ai_avatar"}
            for comment in comments
        ],
    }


def market_actor_comment_budget(actor_id: str, *, at: datetime | None = None) -> dict[str, Any]:
    day = (at or datetime.now(UTC)).date().isoformat()
    with connection() as db:
        actor_count = int(
            db.execute(
                """
                SELECT COUNT(*) FROM market_actor_comments
                WHERE actor_id=? AND created_at>=?
                """,
                (actor_id, f"{day}T00:00:00"),
            ).fetchone()[0]
        )
        row = db.execute(
            "SELECT value FROM worker_state WHERE key=?",
            (f"actor_comments:{day}",),
        ).fetchone()
    global_count = int(row["value"]) if row is not None else 0
    remaining = max(0, ACTOR_COMMENT_PER_DAY - actor_count)
    global_remaining = max(0, ACTOR_COMMENT_DAILY_LIMIT - global_count)
    return {
        "allowed": remaining > 0 and global_remaining > 0,
        "actor_remaining": remaining,
        "global_remaining": global_remaining,
    }


def record_market_actor_comment(*, at: datetime | None = None) -> None:
    day = (at or datetime.now(UTC)).date().isoformat()
    timestamp = _iso(at)
    with connection() as db:
        row = db.execute(
            "SELECT value FROM worker_state WHERE key=?", (f"actor_comments:{day}",)
        ).fetchone()
        count = int(row["value"]) + 1 if row is not None else 1
        db.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (f"actor_comments:{day}", str(count), timestamp),
        )


def derive_market_actors(
    database: Any = None, *, at: datetime | None = None
) -> dict[str, int]:
    return {
        "stock": derive_stock_actors(database, at=at),
        "coin": derive_coin_actors(database, at=at),
    }
