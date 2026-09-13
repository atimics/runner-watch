"""Participant identity records: entities, references, claims, resolutions.

Implements the persistence contract in
docs/research/pseudonymous-identity-design.md (backlog #248). An entity is the
stable description of a person, organization or provisional group; references
are the observed wallets, identifiers and names attached to it; claims are the
dated statements between them. Each record keeps an opaque ID that stays fixed
while names, portraits and membership change around it.

Two clocks are kept everywhere: the period a statement describes
(`valid_from`/`observed_from`, unknown start dates stay unknown) and when RATi
learned it (`learned_at`).

Raw identity research stays in the service layer. Public presenters receive
only selected facts (see `public_associations`).
"""

from __future__ import annotations

import hashlib
from typing import Any

from runner_web import db as database_module

CLAIM_POLICY_VERSION = "identity-claims-v1"

ENTITY_KINDS = ("person", "organization", "provisional_group")
REFERENCE_KINDS = ("name", "wallet", "filing", "league", "tag", "legacy_actor")
CLAIM_RELATIONSHIPS = (
    "same_participant",
    "shared_operator",
    "control",
    "ownership",
    "custody",
    "funding",
    "employment",
    "sponsorship",
)
CLAIM_STATES = ("proposed", "accepted", "disputed", "retracted")

RELATIONSHIP_LABELS = {
    "same_participant": "Same participant",
    "shared_operator": "Shared operator",
    "control": "Controlled by",
    "ownership": "Owned by",
    "custody": "Custody of",
    "funding": "Funded by",
    "employment": "Works with",
    "sponsorship": "Sponsored by",
}


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _now() -> str:
    from runner_web.outcomes import iso

    return iso()


def ensure_entity(
    kind: str,
    *,
    dedupe_key: str | None = None,
    created_at: str | None = None,
    connection: Any = None,
) -> dict[str, Any]:
    """Return the entity for a dedupe key, creating it once.

    Repeated ingestion converges on one entity: names, wallets and labels can
    change around the opaque ID, and the dedupe key is only a joining hint for
    ingestion, never the public identity.
    """

    if kind not in ENTITY_KINDS:
        raise ValueError(f"unknown entity kind: {kind}")
    if connection is None:
        with database_module.connection() as opened:
            return ensure_entity(
                kind, dedupe_key=dedupe_key, created_at=created_at, connection=opened
            )
    key = _clean(dedupe_key)
    stamp = _clean(created_at) or _now()
    row = (
        connection.execute(
            "SELECT * FROM participant_entities WHERE dedupe_key=?", (key,)
        ).fetchone()
        if key
        else None
    )
    if row is None:
        entity_id = "pe-" + hashlib.sha256(
            (key or f"{kind}:{stamp}").encode()
        ).hexdigest()[:24]
        connection.execute(
            """
            INSERT INTO participant_entities(id,kind,dedupe_key,created_at,updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(dedupe_key) DO UPDATE SET kind=excluded.kind
            """,
            (entity_id, kind, key, stamp, stamp),
        )
        row = connection.execute(
            "SELECT * FROM participant_entities WHERE id=?", (entity_id,)
        ).fetchone()
    return dict(row)


def attach_reference(
    entity_id: str,
    kind: str,
    value: str,
    *,
    chain: str | None = None,
    network: str | None = None,
    issuing_system: str | None = None,
    observed_from: str | None = None,
    observed_until: str | None = None,
    learned_at: str | None = None,
    source_kind: str | None = None,
    source_id: str | None = None,
    connection: Any = None,
) -> str:
    """Attach one observed reference, idempotently.

    The same address text on two networks or the same identifier from two
    issuing systems are distinct references; the same observation recorded
    twice is one reference.
    """

    if kind not in REFERENCE_KINDS:
        raise ValueError(f"unknown reference kind: {kind}")
    clean_value = _clean(value)
    if not clean_value:
        raise ValueError("reference value is required")
    if connection is None:
        with database_module.connection() as opened:
            return attach_reference(
                entity_id,
                kind,
                clean_value,
                chain=chain,
                network=network,
                issuing_system=issuing_system,
                observed_from=observed_from,
                observed_until=observed_until,
                learned_at=learned_at,
                source_kind=source_kind,
                source_id=source_id,
                connection=opened,
            )
    stamp = _clean(learned_at) or _now()
    reference_id = "pr-" + hashlib.sha256(
        "|".join(
            [
                entity_id,
                kind,
                clean_value,
                _clean(chain) or "",
                _clean(issuing_system) or "",
            ]
        ).encode()
    ).hexdigest()[:24]
    connection.execute(
        """
        INSERT INTO participant_references(
            id,entity_id,kind,value,chain,network,issuing_system,
            observed_from,observed_until,learned_at,source_kind,source_id
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO NOTHING
        """,
        (
            reference_id,
            entity_id,
            kind,
            clean_value,
            _clean(chain),
            _clean(network),
            _clean(issuing_system),
            observed_from,
            observed_until,
            stamp,
            _clean(source_kind),
            _clean(source_id),
        ),
    )
    return reference_id


def record_claim(
    subject_entity_id: str,
    relationship: str,
    *,
    object_entity_id: str | None = None,
    object_reference_id: str | None = None,
    state: str = "proposed",
    valid_from: str | None = None,
    valid_until: str | None = None,
    learned_at: str | None = None,
    evidence_kind: str | None = None,
    evidence_id: str | None = None,
    origin: str | None = None,
    review_note: str | None = None,
    supersedes_claim_id: str | None = None,
    connection: Any = None,
) -> str:
    """Record one dated identity or relationship claim.

    The claim ID derives from its content, so recording the same finding twice
    converges on one row instead of duplicating history. The acceptance-policy
    version is stamped on every row.
    """

    if relationship not in CLAIM_RELATIONSHIPS:
        raise ValueError(f"unknown relationship: {relationship}")
    if state not in CLAIM_STATES:
        raise ValueError(f"unknown claim state: {state}")
    if object_entity_id is None and object_reference_id is None:
        raise ValueError("a claim needs an entity or reference object")
    if connection is None:
        with database_module.connection() as opened:
            return record_claim(
                subject_entity_id,
                relationship,
                object_entity_id=object_entity_id,
                object_reference_id=object_reference_id,
                state=state,
                valid_from=valid_from,
                valid_until=valid_until,
                learned_at=learned_at,
                evidence_kind=evidence_kind,
                evidence_id=evidence_id,
                origin=origin,
                review_note=review_note,
                supersedes_claim_id=supersedes_claim_id,
                connection=opened,
            )
    stamp = _clean(learned_at) or _now()
    claim_id = "pc-" + hashlib.sha256(
        "|".join(
            [
                subject_entity_id,
                relationship,
                object_entity_id or "",
                object_reference_id or "",
                _clean(valid_from) or "",
                stamp,
                _clean(origin) or "",
            ]
        ).encode()
    ).hexdigest()[:24]
    connection.execute(
        """
        INSERT INTO participant_claims(
            id,subject_entity_id,relationship,object_entity_id,object_reference_id,
            state,valid_from,valid_until,learned_at,evidence_kind,evidence_id,origin,
            policy_version,review_note,supersedes_claim_id,created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at
        """,
        (
            claim_id,
            subject_entity_id,
            relationship,
            object_entity_id,
            object_reference_id,
            state,
            _clean(valid_from),
            _clean(valid_until),
            stamp,
            _clean(evidence_kind),
            _clean(evidence_id),
            _clean(origin),
            CLAIM_POLICY_VERSION,
            review_note,
            supersedes_claim_id,
            stamp,
            stamp,
        ),
    )
    return claim_id


def review_claim(claim_id: str, state: str, *, note: str | None = None) -> None:
    """Move a claim through accepted/disputed/retracted with a recorded reason."""

    if state not in {"accepted", "disputed", "retracted"}:
        raise ValueError(f"review cannot set state {state}")
    with database_module.connection() as database:
        row = database.execute(
            "SELECT subject_entity_id FROM participant_claims WHERE id=?", (claim_id,)
        ).fetchone()
        if not row:
            raise ValueError("unknown claim")
        database.execute(
            "UPDATE participant_claims SET state=?,review_note=?,updated_at=? WHERE id=?",
            (state, _clean(note), _now(), claim_id),
        )
        _bump_entity_revision(database, str(row["subject_entity_id"]))


def _bump_entity_revision(database: Any, entity_id: str) -> None:
    database.execute(
        "UPDATE participant_entities SET revision=revision+1,updated_at=? WHERE id=?",
        (_now(), entity_id),
    )


def _accepted_alias_groups(database: Any) -> dict[str, set[str]]:
    """Union entities joined by accepted same-participant claims.

    Equivalence applies only to compatible entity kinds: a person cannot merge
    into an organization through a name match.
    """

    rows = database.execute(
        """
        SELECT c.subject_entity_id AS left_id,c.object_entity_id AS right_id,
               s.kind AS left_kind,o.kind AS right_kind
        FROM participant_claims c
        JOIN participant_entities s ON s.id=c.subject_entity_id
        JOIN participant_entities o ON o.id=c.object_entity_id
        WHERE c.relationship='same_participant' AND c.state='accepted'
        """
    ).fetchall()
    groups: dict[str, set[str]] = {}
    for row in rows:
        left, right = str(row["left_id"]), str(row["right_id"])
        if row["left_kind"] != row["right_kind"]:
            continue
        union = groups.pop(left, {left}) | groups.pop(right, {right})
        for member in union:
            groups[member] = union
    return groups


def _group_revision(database: Any, member_ids: set[str]) -> str:
    marks = sorted(member_ids)
    stamps = database.execute(
        f"""
        SELECT COALESCE(MAX(latest),'') FROM (
            SELECT MAX(updated_at) AS latest FROM participant_entities
            WHERE id IN ({",".join("?" * len(marks))})
            UNION ALL
            SELECT MAX(updated_at) FROM participant_claims
            WHERE state='accepted' AND (subject_entity_id IN ({",".join("?" * len(marks))})
                OR object_entity_id IN ({",".join("?" * len(marks))}))
        )
        """,
        (*marks, *marks, *marks),
    ).fetchone()
    marker = str(stamps[0] or "")
    return f"{len(marks)}:{hashlib.sha256(marker.encode()).hexdigest()[:12]}"


def entity_group(entity_id: str) -> dict[str, Any] | None:
    """The current understanding of one entity: its alias group and revision."""

    with database_module.connection() as database:
        entity = database.execute(
            "SELECT * FROM participant_entities WHERE id=?", (entity_id,)
        ).fetchone()
        if not entity:
            return None
        members = sorted(
            _accepted_alias_groups(database).get(str(entity["id"]), {str(entity["id"])})
        )
        placeholders = ",".join("?" * len(members))
        references = database.execute(
            f"""
            SELECT kind,value,chain,network,issuing_system,observed_from,observed_until,
                   review_status
            FROM participant_references WHERE entity_id IN ({placeholders})
            ORDER BY kind,value
            """,
            members,
        ).fetchall()
        associations = public_associations(entity_id, connection=database)
        return {
            "entity_ids": members,
            "kind": str(entity["kind"]),
            "revision": _group_revision(database, set(members)),
            "references": [
                {
                    "kind": str(row["kind"]),
                    "value": str(row["value"]),
                    "chain": row["chain"],
                    "network": row["network"],
                    "issuing_system": row["issuing_system"],
                    "observed_from": row["observed_from"],
                    "observed_until": row["observed_until"],
                    "review_status": str(row["review_status"]),
                }
                for row in references
            ],
            "associations": associations,
        }


def public_associations(entity_id: str, *, connection: Any = None) -> list[dict[str, Any]]:
    """Accepted or disputed relationship wording; no raw identifiers.

    The relationship label, target kind, whether the period is current, and the
    review state carry the meaning. Entity IDs, reference values and evidence
    stay in the service layer.
    """

    if connection is None:
        with database_module.connection() as opened:
            return public_associations(entity_id, connection=opened)
    rows = connection.execute(
        """
        SELECT c.relationship,c.state,c.valid_until,o.kind AS object_kind,
               r.kind AS reference_kind
        FROM participant_claims c
        LEFT JOIN participant_entities o ON o.id=c.object_entity_id
        LEFT JOIN participant_references r ON r.id=c.object_reference_id
        WHERE c.subject_entity_id=? AND c.state IN ('accepted','disputed')
        ORDER BY c.state,c.learned_at DESC
        """,
        (entity_id,),
    ).fetchall()
    return [
        {
            "relationship": str(row["relationship"]),
            "label": RELATIONSHIP_LABELS.get(str(row["relationship"]), ""),
            "state": str(row["state"]),
            "target_kind": str(row["reference_kind"] or row["object_kind"] or ""),
            "current": row["valid_until"] is None,
        }
        for row in rows
    ]


def identity_reconciliation_queue(*, limit: int = 50) -> dict[str, Any]:
    """Ambiguous records awaiting research: name collisions and legacy groups.

    Two person entities sharing one normalized name reference, or provisional
    wallet groups whose membership predates the identity model, land here until
    their underlying observations are assigned.
    """

    with database_module.connection() as database:
        collisions = database.execute(
            """
            SELECT LOWER(r.value) AS name,COUNT(DISTINCT r.entity_id) AS entities,
                   GROUP_CONCAT(DISTINCT r.entity_id) AS entity_ids
            FROM participant_references r
            JOIN participant_entities e ON e.id=r.entity_id AND e.kind='person'
            WHERE r.kind='name' AND r.review_status!='disputed'
            GROUP BY LOWER(r.value) HAVING COUNT(DISTINCT r.entity_id)>1
            ORDER BY entities DESC,name LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        legacy_groups = database.execute(
            """
            SELECT e.id AS entity_id,
                   (SELECT COUNT(*) FROM participant_references r
                    WHERE r.entity_id=e.id AND r.kind='wallet') AS wallets
            FROM participant_entities e
            WHERE e.kind='provisional_group' AND e.status='active'
            ORDER BY e.updated_at DESC LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
    return {
        "name_collisions": [
            {
                "name": str(row["name"]),
                "entity_ids": [part for part in str(row["entity_ids"] or "").split(",") if part],
            }
            for row in collisions
        ],
        "provisional_groups": [
            {"entity_id": str(row["entity_id"]), "wallets": int(row["wallets"])}
            for row in legacy_groups
        ],
    }


def link_market_actors(database: Any) -> None:
    """Attach every actor without an entity to one, plus its wallet references.

    Existing handles, avatars and deep links stay on `market_actors`; the
    entity layer attaches alongside them. Coin cluster members become dated
    wallet references on the actor's entity.
    """

    rows = database.execute(
        "SELECT id,stable_key,kind,domain,created_at FROM market_actors WHERE entity_id IS NULL"
    ).fetchall()
    for row in rows:
        stamp = str(row["created_at"] or _now())
        entity = ensure_entity(
            "provisional_group" if row["domain"] == "coin" else "person",
            dedupe_key=f"market-actor:{row['stable_key']}",
            created_at=stamp,
            connection=database,
        )
        database.execute(
            "UPDATE market_actors SET entity_id=? WHERE id=? AND entity_id IS NULL",
            (entity["id"], row["id"]),
        )
        attach_reference(
            entity["id"],
            "legacy_actor",
            str(row["stable_key"]),
            learned_at=stamp,
            source_kind="ingest",
            source_id=str(row["id"]),
            connection=database,
        )
    members = database.execute(
        """
        SELECT m.actor_id,m.wallet,m.evidence_kind,m.evidence_id,m.created_at
        FROM actor_cluster_members m
        JOIN market_actors a ON a.id=m.actor_id
        WHERE a.domain='coin' AND a.entity_id IS NOT NULL
        """
    ).fetchall()
    for member in members:
        holder = database.execute(
            "SELECT entity_id,created_at FROM market_actors WHERE id=?", (member["actor_id"],)
        ).fetchone()
        if not holder or not holder["entity_id"]:
            continue
        attach_reference(
            str(holder["entity_id"]),
            "wallet",
            str(member["wallet"]),
            chain="solana",
            network="solana",
            learned_at=str(member["created_at"] or holder["created_at"] or _now()),
            source_kind=str(member["evidence_kind"] or "chain"),
            source_id=member["evidence_id"],
            connection=database,
        )
