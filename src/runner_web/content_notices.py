from __future__ import annotations

import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from runner_web.db import connection

NOTICE_LABELS = {
    "correction": "Correction",
    "holdings": "Holdings",
    "compensation": "Compensation",
    "issuer_relationship": "Issuer relationship",
    "sponsorship": "Sponsorship",
}
DISCLOSURE_KINDS = tuple(kind for kind in NOTICE_LABELS if kind != "correction")


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    cleaned = " ".join(value.split())
    if not 3 <= len(cleaned) <= maximum:
        raise ValueError(f"{field} must contain 3 to {maximum} characters")
    return cleaned


def disclosure_input(payload: Any) -> tuple[str, str] | None:
    if not isinstance(payload, dict):
        raise ValueError("Disclosure fields must be a JSON object")
    kind = payload.get("disclosure_kind") or ""
    text = payload.get("disclosure") or ""
    if kind == "" and text == "":
        return None
    if kind not in DISCLOSURE_KINDS:
        raise ValueError("Choose a disclosure kind")
    return kind, _text(text, "Disclosure", 1000)


def _public_notice(row: Any) -> dict[str, Any]:
    return {
        key: row[key] for key in ("id", "kind", "text", "reason", "recorded_by", "created_at")
    } | {"label": NOTICE_LABELS[row["kind"]]}


def insert_content_notice(
    database: Any,
    subject: str,
    subject_id: str,
    *,
    kind: str,
    text: str,
    reason: str | None = None,
    recorded_by: str = "operator",
    at: datetime | None = None,
) -> dict[str, Any]:
    if subject not in {"report", "comment"}:
        raise ValueError("Choose report or comment")
    if kind not in NOTICE_LABELS:
        raise ValueError("Choose a supported notice kind")
    if recorded_by not in {"author", "operator"}:
        raise ValueError("Choose author or operator")
    if kind == "correction" and recorded_by != "operator":
        raise ValueError("Content corrections require an operator")
    cleaned = _text(text, "Notice", 4000 if kind == "correction" else 1000)
    clean_reason = _text(reason, "Correction reason", 500) if kind == "correction" else None
    if kind != "correction" and reason is not None:
        raise ValueError("A reason belongs to a correction")
    table = "research_commissions" if subject == "report" else "ticker_comments"
    state = "complete" if subject == "report" else "public"
    target = database.execute(
        f"SELECT id FROM {table} WHERE id=? AND status=?", (subject_id, state)
    ).fetchone()
    if target is None:
        raise KeyError(f"Completed {subject} not found")
    dedup_key = sha256(f"{kind}\0{cleaned}".encode()).hexdigest()
    notice = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "text": cleaned,
        "reason": clean_reason,
        "recorded_by": recorded_by,
        "created_at": (at or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    inserted = database.execute(
        """
        INSERT INTO content_notices(
            id,report_id,comment_id,kind,text,reason,recorded_by,dedup_key,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING
        """,
        (
            notice["id"],
            subject_id if subject == "report" else None,
            subject_id if subject == "comment" else None,
            kind,
            cleaned,
            clean_reason,
            recorded_by,
            dedup_key,
            notice["created_at"],
        ),
    )
    if not inserted.rowcount:
        column = "report_id" if subject == "report" else "comment_id"
        previous = database.execute(
            f"SELECT * FROM content_notices WHERE {column}=? AND dedup_key=? "
            "AND recorded_by='author'",
            (subject_id, dedup_key),
        ).fetchone()
        if previous is None:
            raise ValueError("Could not save this notice")
        return _public_notice(previous)
    return _public_notice(notice)


def record_content_notice(subject: str, target_id: str, **notice: Any) -> dict[str, Any]:
    """Operators identify reports by public ID and comments by their displayed ID."""
    with connection() as database:
        if subject == "report":
            row = database.execute(
                "SELECT id FROM research_commissions WHERE public_id=?", (target_id,)
            ).fetchone()
            if row is None:
                raise KeyError("Report not found")
            target_id = str(row["id"])
        return insert_content_notice(database, subject, target_id, **notice)


def notices_for_content(subject: str, subject_ids: list[str]) -> dict[str, dict[str, list]]:
    if subject not in {"report", "comment"}:
        raise ValueError("Choose report or comment")
    result = {key: {"corrections": [], "disclosures": []} for key in subject_ids}
    if not result:
        return result
    column = "report_id" if subject == "report" else "comment_id"
    with connection() as database:
        rows = database.execute(
            f"SELECT * FROM content_notices WHERE {column} IN "
            f"({','.join('?' for _ in result)}) ORDER BY created_at,id",
            tuple(result),
        ).fetchall()
    for row in rows:
        group = "corrections" if row["kind"] == "correction" else "disclosures"
        result[row[column]][group].append(_public_notice(row))
    return result


def attach_comment_notices(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    notices = notices_for_content("comment", [str(row["id"]) for row in comments])
    return [{**row, **notices[str(row["id"])]} for row in comments]


def report_share_metadata(report: dict[str, Any]) -> dict[str, str]:
    corrections = report.get("corrections") or []
    disclosures = report.get("disclosures") or []
    latest_correction = corrections[-1] if corrections else None
    labels = (["Correction"] if corrections else []) + (["Disclosure"] if disclosures else [])
    label = " · ".join(labels)
    headline = str(report.get("headline") or "Research report")
    summary = str(latest_correction["text"] if latest_correction else report.get("summary") or "")
    title = " · ".join([str(report["ticker"]), *labels, *([] if corrections else [headline])])
    return {
        "share_title": title,
        "share_summary": f"{label}: {summary}" if label else summary,
        "share_excerpt": str(latest_correction["text"]) if latest_correction else headline,
        "share_notice_label": label,
    }
