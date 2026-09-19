"""Ticker story identity: question, revisit trigger, versioned outcome (#245).

A ticker can carry several stories. Each story is one open question about a
typed subject with a bounded review time; updates are versioned, corrections
preserve the original, and Calls or follows link to the story while keeping
their own identities. Review time is never coupled to Call settlement — those
rules belong to their own services.

Internal identifiers, source receipts and research controls stay here. The
public presenter receives `public_story()` output only.
"""

from __future__ import annotations

import hashlib
from typing import Any

from runner_web import db as database_module

STORY_KINDS = ("report", "fact", "correction", "reveal", "review", "close")


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _now() -> str:
    from runner_web.outcomes import iso

    return iso()


def _story_key(market: str, subject_kind: str, subject_key: str, question: str) -> str:
    normalized = " ".join(question.split()).casefold()
    digest = hashlib.sha256(
        "|".join([market, subject_kind, subject_key, normalized]).encode()
    ).hexdigest()[:24]
    return "st-" + digest


def open_story(
    market: str,
    subject_kind: str,
    subject_key: str,
    question: str,
    *,
    revisit_trigger: str | None = None,
    next_review_at: str | None = None,
    reference: tuple[str, str] | None = None,
    at: str | None = None,
    connection: Any = None,
) -> dict[str, Any]:
    """Open the story for one question about one subject, idempotently.

    Repeated updates converge on one versioned story: the same market, subject
    and question resolve to the existing row. Two different questions on the
    same ticker are two simultaneous stories.
    """

    if market not in {"stocks", "memecoins", "sports"}:
        raise ValueError(f"unknown market: {market}")
    if subject_kind not in {"ticker", "coin", "game"}:
        raise ValueError(f"unknown subject kind: {subject_kind}")
    clean_question = _clean(question)
    if not clean_question:
        raise ValueError("a story needs its question")
    if connection is None:
        with database_module.connection() as opened:
            return open_story(
                market,
                subject_kind,
                subject_key,
                clean_question,
                revisit_trigger=revisit_trigger,
                next_review_at=next_review_at,
                reference=reference,
                at=at,
                connection=opened,
            )
    stamp = _clean(at) or _now()
    story_key = _story_key(market, subject_kind, subject_key, clean_question)
    existing = connection.execute(
        "SELECT * FROM ticker_stories WHERE story_key=?", (story_key,)
    ).fetchone()
    if existing:
        return dict(existing)
    reference_kind, reference_id = reference or (None, None)
    connection.execute(
        """
        INSERT INTO ticker_stories(
            id,market,subject_kind,subject_key,story_key,question,opened_at,
            opening_reference_kind,opening_reference_id,revisit_trigger,next_review_at,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            story_key,
            market,
            subject_kind,
            subject_key,
            story_key,
            clean_question,
            stamp,
            _clean(reference_kind) if reference_kind else None,
            _clean(reference_id) if reference_id else None,
            _clean(revisit_trigger),
            _clean(next_review_at),
            stamp,
            stamp,
        ),
    )
    row = connection.execute("SELECT * FROM ticker_stories WHERE id=?", (story_key,)).fetchone()
    return dict(row)


def add_story_update(
    story_id: str,
    kind: str,
    *,
    note: str | None = None,
    reference: tuple[str, str] | None = None,
    correction_of_update_id: str | None = None,
    applies_from: str | None = None,
    at: str | None = None,
    connection: Any = None,
) -> dict[str, Any]:
    """Append one versioned update. Corrections never touch the original."""

    if kind not in STORY_KINDS:
        raise ValueError(f"unknown update kind: {kind}")
    if connection is None:
        with database_module.connection() as opened:
            return add_story_update(
                story_id,
                kind,
                note=note,
                reference=reference,
                correction_of_update_id=correction_of_update_id,
                applies_from=applies_from,
                at=at,
                connection=opened,
            )
    stamp = _clean(at) or _now()
    version_row = connection.execute(
        "SELECT COALESCE(MAX(version),0) AS version FROM ticker_story_updates WHERE story_id=?",
        (story_id,),
    ).fetchone()
    version = int(version_row["version"]) + 1
    update_id = (
        "su-"
        + hashlib.sha256(
            "|".join([story_id, str(version), kind, _clean(note) or "", stamp]).encode()
        ).hexdigest()[:24]
    )
    reference_kind, reference_id = reference or (None, None)
    connection.execute(
        """
        INSERT INTO ticker_story_updates(
            id,story_id,version,kind,note,reference_kind,reference_id,
            correction_of_update_id,applies_from,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(story_id,version) DO UPDATE SET note=excluded.note
        """,
        (
            update_id,
            story_id,
            version,
            kind,
            _clean(note),
            _clean(reference_kind) if reference_kind else None,
            _clean(reference_id) if reference_id else None,
            correction_of_update_id,
            _clean(applies_from),
            stamp,
        ),
    )
    # New evidence reopens the question; the resolved outcome stays in history
    # through the earlier versions.
    connection.execute(
        """
        UPDATE ticker_stories
        SET current_version=?,updated_at=?,status='open',outcome=NULL,resolved_at=NULL
        WHERE id=?
        """,
        (version, stamp, story_id),
    )
    row = connection.execute(
        "SELECT * FROM ticker_story_updates WHERE id=?", (update_id,)
    ).fetchone()
    return dict(row)


def resolve_story(
    story_id: str,
    outcome: str,
    *,
    at: str | None = None,
    connection: Any = None,
) -> None:
    """Close the question with a plain outcome; the review stays bounded."""

    if connection is None:
        with database_module.connection() as opened:
            resolve_story(story_id, outcome, at=at, connection=opened)
            return
    stamp = _clean(at) or _now()
    clean_outcome = _clean(outcome)
    if not clean_outcome:
        raise ValueError("a resolution needs its outcome")
    version_row = connection.execute(
        "SELECT COALESCE(MAX(version),0) AS version FROM ticker_story_updates WHERE story_id=?",
        (story_id,),
    ).fetchone()
    version = int(version_row["version"]) + 1
    connection.execute(
        """
        INSERT INTO ticker_story_updates(
            id,story_id,version,kind,note,created_at
        ) VALUES(?,?,?,?,?,?)
        ON CONFLICT(story_id,version) DO UPDATE SET note=excluded.note
        """,
        (
            "su-" + hashlib.sha256(f"{story_id}|{version}|close|{stamp}".encode()).hexdigest()[:24],
            story_id,
            version,
            "close",
            clean_outcome,
            stamp,
        ),
    )
    connection.execute(
        """
        UPDATE ticker_stories
        SET status='resolved',outcome=?,resolved_at=?,current_version=?,updated_at=?
        WHERE id=?
        """,
        (clean_outcome, stamp, version, stamp, story_id),
    )


def attach_call(story_id: str, call_id: str, *, connection: Any = None) -> bool:
    """Link one Call to the story; the Call keeps its own identity and rules."""

    return _attach_link(story_id, "call", call_id, connection=connection)


def attach_follow(story_id: str, follow_id: str, *, connection: Any = None) -> bool:
    """Link one explicit follow (arrives with #242) to the story."""

    return _attach_link(story_id, "follow", follow_id, connection=connection)


def _attach_link(story_id: str, kind: str, link_id: str, *, connection: Any) -> bool:
    if connection is None:
        with database_module.connection() as opened:
            return _attach_link(story_id, kind, link_id, connection=opened)
    clean = _clean(link_id)
    if not clean:
        raise ValueError("a link needs its target id")
    cursor = connection.execute(
        """
        INSERT INTO ticker_story_links(story_id,link_kind,link_id,created_at)
        VALUES(?,?,?,?)
        ON CONFLICT(story_id,link_kind,link_id) DO NOTHING
        """,
        (story_id, kind, clean, _now()),
    )
    return bool(getattr(cursor, "rowcount", 0))


def story_links(story_id: str) -> dict[str, list[str]]:
    with database_module.connection() as database:
        rows = database.execute(
            "SELECT link_kind,link_id FROM ticker_story_links WHERE story_id=? "
            "ORDER BY created_at,link_id",
            (story_id,),
        ).fetchall()
    links: dict[str, list[str]] = {"call": [], "follow": []}
    for row in rows:
        links.setdefault(str(row["link_kind"]), []).append(str(row["link_id"]))
    return links


def stories_by_subject(market: str, subject_keys: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Public story payloads grouped by subject, open stories first."""

    clean = sorted({key for key in subject_keys if _clean(key)})
    if not clean:
        return {}
    try:
        with database_module.connection() as database:
            placeholders = ",".join("?" * len(clean))
            rows = database.execute(
                f"""
                SELECT * FROM ticker_stories
                WHERE market=? AND subject_key IN ({placeholders})
                ORDER BY status,updated_at DESC
                """,
                (market, *clean),
            ).fetchall()
    except Exception:
        # Story state is additive: a board still renders when the story store
        # is unavailable (fresh fixtures, rollback windows).
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        payload = dict(row)
        payload.pop("id", None)
        payload.pop("story_key", None)
        payload.pop("opening_reference_kind", None)
        payload.pop("opening_reference_id", None)
        grouped.setdefault(str(row["subject_key"]), []).append(payload)
    return grouped


def public_story(market: str, subject_key: str) -> dict[str, Any] | None:
    """One public question, next-update cue and outcome for a subject."""

    grouped = stories_by_subject(market, [subject_key])
    stories = grouped.get(subject_key) or []
    if not stories:
        return None
    story = stories[0]
    return {
        "question": story["question"],
        "status": story["status"],
        "outcome": story["outcome"],
        "next_review_at": story["next_review_at"],
        "revisit_trigger": story["revisit_trigger"],
        "version": story["current_version"],
        "updated_at": story["updated_at"],
    }
