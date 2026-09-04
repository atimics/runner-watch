from __future__ import annotations

import json
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_web.db import connection

CASE_STATUSES = {"active", "closed", "archived"}
DEFAULT_CASE_HORIZON_MINUTES = 5 * 24 * 60
MAX_CASE_HORIZON_MINUTES = 180 * 24 * 60


def infer_horizon_minutes(comment: str) -> int:


    text = " ".join(comment.casefold().split())
    explicit = re.search(r"(?<![a-z0-9])(\d{1,3})\s*(h|hr|hrs|d|day|days|w|wk|wks)(?![a-z])", text)
    if explicit:
        value = int(explicit.group(1))
        unit = explicit.group(2)
        multiplier = 60 if unit in {"h", "hr", "hrs"} else 1440
        if unit in {"w", "wk", "wks"}:
            multiplier = 7 * 1440
        return max(60, min(value * multiplier, MAX_CASE_HORIZON_MINUTES))
    phrases = (
        (("six months", "half year"), 180 * 1440),
        (("quarter", "three months", "3 months"), 90 * 1440),
        (("this month", "next month", "one month", "month"), 30 * 1440),
        (("two weeks", "fortnight"), 14 * 1440),
        (("this week", "next week", "one week", "week"), 7 * 1440),
        (("two days",), 2 * 1440),
        (("tomorrow", "next day", "one day"), 1440),
        (("intraday", "today"), 390),
        (("next hour", "one hour", "hour"), 60),
    )
    for needles, minutes in phrases:
        if any(needle in text for needle in needles):
            return minutes
    return DEFAULT_CASE_HORIZON_MINUTES


def _iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_dict(row: Any) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if str(item).strip()] if isinstance(parsed, list) else []


def _json_array(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _clean_items(items: list[str] | None, *, limit: int = 12) -> list[str]:
    output: list[str] = []
    for raw in items or []:
        item = " ".join(str(raw).split())[:500]
        if item and item not in output:
            output.append(item)
        if len(output) >= limit:
            break
    return output


def _public_case(row: Any) -> dict[str, Any]:
    item = _row_dict(row)
    item.pop("user_id", None)
    item["risks"] = _json_list(item.pop("risks_json", "[]"))
    item["open_questions"] = _json_list(item.pop("questions_json", "[]"))
    if item.get("confidence") is not None:
        item["confidence"] = float(item["confidence"])
    item["horizon_minutes"] = int(item["horizon_minutes"])
    if item.get("reference_price") is not None:
        item["reference_price"] = float(item["reference_price"])
    for key in (
        "outcome_end_price",
        "outcome_return_pct",
        "outcome_max_favorable_pct",
        "outcome_max_adverse_pct",
    ):
        if item.get(key) is not None:
            item[key] = float(item[key])
    if "citations_json" in item:
        item["latest_citations"] = _json_array(item.pop("citations_json"))
    return item


def get_case(user_id: str, public_id: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """
            WITH latest_update AS (
                SELECT *,ROW_NUMBER() OVER (
                    PARTITION BY case_id ORDER BY created_at DESC,id DESC
                ) AS position
                FROM thesis_case_updates
            )
            SELECT c.*,u.direction AS latest_direction,u.summary AS latest_summary,
                   u.recommended_action AS latest_action,
                   u.citations_json,u.created_at AS latest_update_at,
                   o.status AS outcome_status,o.due_at AS outcome_due_at,
                   o.end_price AS outcome_end_price,o.observed_at AS outcome_observed_at,
                   o.return_pct AS outcome_return_pct,o.return_direction AS outcome_direction,
                   o.max_favorable_pct AS outcome_max_favorable_pct,
                   o.max_adverse_pct AS outcome_max_adverse_pct,
                   ca.name AS source_pseudonym,
                   (SELECT COUNT(*) FROM thesis_case_revisions r WHERE r.case_id=c.id)
                       AS revision_count
            FROM thesis_cases c
            LEFT JOIN latest_update u ON u.case_id=c.id AND u.position=1
            LEFT JOIN thesis_case_outcomes o ON o.case_id=c.id
            LEFT JOIN ticker_comments sc ON sc.id=c.source_comment_id
            LEFT JOIN comment_avatars ca ON ca.user_id=sc.user_id
            WHERE c.user_id=? AND c.public_id=?
            """,
            (user_id, public_id),
        ).fetchone()
    return _public_case(row) if row else None


def latest_case_for_ticker(user_id: str, ticker: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT public_id FROM thesis_cases
            WHERE user_id=? AND ticker=? AND status='active'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (user_id, ticker),
        ).fetchone()
    return get_case(user_id, str(row["public_id"])) if row else None


def list_cases(
    user_id: str,
    *,
    include_inactive: bool = False,
    include_recent_closed: bool = False,
) -> list[dict[str, Any]]:
    if include_inactive:
        status_clause = ""
        parameters: tuple[Any, ...] = (user_id,)
    elif include_recent_closed:
        status_clause = "AND (c.status='active' OR c.closed_at>=?)"
        parameters = (user_id, (datetime.now(UTC) - timedelta(days=30)).isoformat())
    else:
        status_clause = "AND c.status='active'"
        parameters = (user_id,)
    with connection() as db:
        rows = db.execute(
            f"""
            WITH latest_update AS (
                SELECT *,ROW_NUMBER() OVER (
                    PARTITION BY case_id ORDER BY created_at DESC,id DESC
                ) AS position
                FROM thesis_case_updates
            )
            SELECT c.*,u.direction AS latest_direction,u.summary AS latest_summary,
                   u.recommended_action AS latest_action,
                   u.citations_json,u.created_at AS latest_update_at,
                   o.status AS outcome_status,o.due_at AS outcome_due_at,
                   o.end_price AS outcome_end_price,o.observed_at AS outcome_observed_at,
                   o.return_pct AS outcome_return_pct,o.return_direction AS outcome_direction,
                   o.max_favorable_pct AS outcome_max_favorable_pct,
                   o.max_adverse_pct AS outcome_max_adverse_pct,
                   ca.name AS source_pseudonym,
                   (SELECT COUNT(*) FROM thesis_case_revisions r WHERE r.case_id=c.id)
                       AS revision_count
            FROM thesis_cases c
            LEFT JOIN latest_update u ON u.case_id=c.id AND u.position=1
            LEFT JOIN thesis_case_outcomes o ON o.case_id=c.id
            LEFT JOIN ticker_comments sc ON sc.id=c.source_comment_id
            LEFT JOIN comment_avatars ca ON ca.user_id=sc.user_id
            WHERE c.user_id=? {status_clause}
            ORDER BY COALESCE(u.created_at,c.updated_at) DESC,c.updated_at DESC
            """,
            parameters,
        ).fetchall()
    return [_public_case(row) for row in rows]


def case_revisions(user_id: str, public_id: str) -> list[dict[str, Any]] | None:
    with connection() as db:
        owned = db.execute(
            "SELECT id FROM thesis_cases WHERE user_id=? AND public_id=?",
            (user_id, public_id),
        ).fetchone()
        if not owned:
            return None
        rows = db.execute(
            """
            SELECT * FROM thesis_case_revisions
            WHERE case_id=? ORDER BY revision_no DESC
            """,
            (owned["id"],),
        ).fetchall()
    return [_public_case(row) for row in rows]


def create_case(
    user_id: str,
    ticker: str,
    *,
    thesis: str,
    horizon_minutes: int,
    reference_price: float | None,
    invalidation: str,
    risks: list[str] | None,
    open_questions: list[str] | None,
    confidence: float | None,
    source_comment_id: str | None = None,
    source_kind: str = "short_note",
) -> dict[str, Any]:
    timestamp = _iso()
    case_id = str(uuid.uuid4())
    public_id = f"case-{secrets.token_urlsafe(9)}"
    clean_risks = _clean_items(risks)
    clean_questions = _clean_items(open_questions)
    risks_json = json.dumps(clean_risks, separators=(",", ":"))
    questions_json = json.dumps(clean_questions, separators=(",", ":"))
    clean_thesis = " ".join(thesis.split())
    clean_invalidation = " ".join(invalidation.split())
    with connection() as db:
        db.execute(
            """
            INSERT INTO thesis_cases(
                id,public_id,user_id,ticker,source_kind,source_comment_id,
                thesis,horizon_minutes,reference_price,
                reference_at,invalidation,risks_json,questions_json,confidence,status,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                case_id,
                public_id,
                user_id,
                ticker,
                source_kind,
                source_comment_id,
                clean_thesis,
                horizon_minutes,
                reference_price,
                timestamp,
                clean_invalidation,
                risks_json,
                questions_json,
                confidence,
                "active",
                timestamp,
                timestamp,
            ),
        )
        db.execute(
            """
            INSERT INTO thesis_case_revisions(
                id,case_id,revision_no,source_comment_id,thesis,horizon_minutes,reference_price,
                reference_at,invalidation,risks_json,questions_json,confidence,status,
                final_outcome,change_note,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                case_id,
                1,
                source_comment_id,
                clean_thesis,
                horizon_minutes,
                reference_price,
                timestamp,
                clean_invalidation,
                risks_json,
                questions_json,
                confidence,
                "active",
                None,
                "View saved",
                timestamp,
            ),
        )
        db.execute(
            """
            INSERT INTO thesis_case_updates(
                id,case_id,kind,direction,summary,recommended_action,
                confidence_before,confidence_after,citations_json,evidence_fingerprint,
                deterministic_veto_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                case_id,
                "created",
                "unchanged",
                "Personal view saved. No new evidence has been reviewed yet.",
                "Watch for evidence that changes the view or its risks.",
                None,
                confidence,
                "[]",
                f"case-created:{case_id}",
                "{}",
                timestamp,
            ),
        )
    created = get_case(user_id, public_id)
    assert created is not None
    return created


def update_case(
    user_id: str,
    public_id: str,
    changes: dict[str, Any],
    *,
    change_note: str,
) -> dict[str, Any] | None:
    with connection() as db:
        current_row = db.execute(
            "SELECT * FROM thesis_cases WHERE user_id=? AND public_id=?",
            (user_id, public_id),
        ).fetchone()
        if not current_row:
            return None
        current = _row_dict(current_row)
        for key in (
            "thesis",
            "horizon_minutes",
            "reference_price",
            "invalidation",
            "confidence",
            "status",
            "final_outcome",
            "source_comment_id",
            "source_kind",
        ):
            if key in changes:
                current[key] = changes[key]
        if "risks" in changes:
            current["risks_json"] = json.dumps(
                _clean_items(changes["risks"]), separators=(",", ":")
            )
        if "open_questions" in changes:
            current["questions_json"] = json.dumps(
                _clean_items(changes["open_questions"]), separators=(",", ":")
            )
        current["thesis"] = " ".join(str(current["thesis"]).split())
        current["invalidation"] = " ".join(str(current["invalidation"]).split())
        status = str(current["status"])
        if status not in CASE_STATUSES:
            raise ValueError("Invalid case status")
        timestamp = _iso()
        closed_at = timestamp if status == "closed" else None
        revision_no = int(
            db.execute(
                "SELECT COALESCE(MAX(revision_no),0)+1 FROM thesis_case_revisions WHERE case_id=?",
                (current["id"],),
            ).fetchone()[0]
        )
        db.execute(
            """
            UPDATE thesis_cases SET
                source_kind=?,source_comment_id=?,thesis=?,horizon_minutes=?,reference_price=?,
                invalidation=?,risks_json=?,questions_json=?,confidence=?,status=?,
                final_outcome=?,updated_at=?,closed_at=?
            WHERE id=?
            """,
            (
                current["source_kind"],
                current.get("source_comment_id"),
                current["thesis"],
                current["horizon_minutes"],
                current.get("reference_price"),
                current["invalidation"],
                current["risks_json"],
                current["questions_json"],
                current["confidence"],
                status,
                current.get("final_outcome"),
                timestamp,
                closed_at,
                current["id"],
            ),
        )
        db.execute(
            """
            INSERT INTO thesis_case_revisions(
                id,case_id,revision_no,source_comment_id,thesis,horizon_minutes,reference_price,
                reference_at,invalidation,risks_json,questions_json,confidence,status,
                final_outcome,change_note,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                current["id"],
                revision_no,
                current.get("source_comment_id"),
                current["thesis"],
                current["horizon_minutes"],
                current.get("reference_price"),
                current["reference_at"],
                current["invalidation"],
                current["risks_json"],
                current["questions_json"],
                current["confidence"],
                status,
                current.get("final_outcome"),
                " ".join(change_note.split())[:500],
                timestamp,
            ),
        )
    return get_case(user_id, public_id)
