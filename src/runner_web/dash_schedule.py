"""Canonical review instants and public labels for Dash's follow-through."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "America/New_York"
MARKER = re.compile(r"\[review:([^\]\r\n]+)\]")
TIME_PROPERTIES = {
    "review_at": {
        "type": "string",
        "description": (
            "Exact review time in ISO 8601 with an explicit UTC offset, "
            "e.g. 2026-11-15T04:30:00+00:00."
        ),
    },
    "review_timezone": {
        "type": "string",
        "description": "IANA timezone for the public review time; defaults to America/New_York.",
    },
    "review_minutes": {"type": "integer", "minimum": 30, "maximum": 1440},
}


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Review time needs an explicit UTC offset.")
    return parsed.astimezone(UTC)


def stored_instant(value) -> datetime | None:
    """Legacy naive timestamps were UTC; malformed records cannot crash the worker."""
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def propose(args: dict, at: datetime, *, max_days: int = 1) -> dict:
    """Resolve absolute or legacy relative input to one aware UTC instant."""
    minutes = args.get("review_minutes")
    if minutes is not None and (type(minutes) is not int or not 30 <= minutes <= 1440):
        raise ValueError("Choose a review time from 30 minutes to one day.")
    value = args.get("review_at")
    if value is not None:
        if not isinstance(value, str):
            raise ValueError("Use an ISO 8601 review time with an explicit UTC offset.")
        try:
            review = instant(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Use an ISO 8601 review time with an explicit UTC offset.") from exc
        if minutes is not None and review != at + timedelta(minutes=minutes):
            raise ValueError("review_at and review_minutes must identify the same time; use one.")
    elif minutes is not None:
        review = at + timedelta(minutes=minutes)
    else:
        raise ValueError("Supply review_at with a UTC offset, or legacy review_minutes.")
    if not at + timedelta(minutes=30) <= review <= at + timedelta(days=max_days):
        horizon = "one day" if max_days == 1 else f"{max_days} days"
        raise ValueError(f"Choose a review time from 30 minutes to {horizon} in the future.")
    timezone = args.get("review_timezone", DEFAULT_TIMEZONE)
    try:
        ZoneInfo(timezone)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Choose a valid IANA review_timezone, such as America/New_York.") from exc
    result = {"review_at": review.astimezone(UTC).isoformat(), "review_timezone": timezone}
    if minutes is not None:
        result["review_minutes"] = minutes
    return result


def label(record: dict) -> str:
    review = stored_instant(record["review_at"]).astimezone(
        ZoneInfo(record.get("review_timezone", DEFAULT_TIMEZONE))
    )
    date = review.strftime("%b %d, %Y").replace(" 0", " ")
    clock = review.strftime("%H:%M:%S" if review.second else "%H:%M")
    return f"{date} at {clock} {review.tzname()}"


def reference(name: str, record: dict) -> dict:
    return {"marker": f"[review:{name}]", "review_at": record["review_at"], "time": label(record)}


def outstanding(record: dict) -> bool:
    promised = stored_instant(record.get("announced_review_at"))
    reviewed = stored_instant(record.get("reviewed_at"))
    return bool(promised and (not reviewed or promised > reviewed))


def preserve_promise(prior: dict, record: dict, at: datetime, *, announced=False) -> dict:
    promised = stored_instant(prior.get("announced_review_at"))
    checked = (
        stored_instant(
            record.get("review_attempted_at")
            or record.get("reviewed_at")
            or record.get("updated_at")
        )
        or at
    )
    if promised and promised > checked and not announced:
        record = {**record, "review_at": prior["review_at"]}
        if record.get("status") == "closed" and prior.get("status") == "watching":
            record.update(status="watching", pending_status="closed")
        for key in ("review_timezone", "announced_review_at", "announced_at"):
            if key in prior:
                record[key] = prior[key]
    return record


def render(text: str, records: dict, at: datetime, *, automatic: bool) -> tuple[str, list[str]]:
    references = list(dict.fromkeys(MARKER.findall(text)))
    if "[review:" in MARKER.sub("", text):
        raise ValueError("Use a complete review marker returned by a saved schedule.")
    for name in references:
        record = records.get(name)
        when = stored_instant(record.get("review_at")) if record else None
        if not when or when <= at or record.get("status") == "closed":
            raise ValueError("Save a future watching review before announcing its marker.")

    def replace(match):
        if not automatic:
            return "I'll revisit this in our next conversation."
        return (
            f"I'll check again around {label(records[match[1]])}. "
            "I'll post if there is a useful update."
        )

    return MARKER.sub(replace, text), references


def finalize(database, scope, result, at):
    """Bind rendered commitments to the winning durable records in the same transaction."""
    draft = result.get("_schedule")
    if not draft:
        return
    from runner_web import dash_direction, dash_research

    rows, records = {}, {}
    for name in draft["references"]:
        key = (
            dash_direction._key(scope, "notes")
            if name == "direction"
            else dash_research._key(scope, name)
        )
        row = database.execute("SELECT value FROM worker_state WHERE key=?", (key,)).fetchone()
        if not row:
            raise ValueError("The announced review was not saved.")
        rows[name] = (key, row["value"])
        records[name] = json.loads(row["value"])
    text, references = render(draft["text"], records, at, automatic=draft["automatic"])
    if len(text.encode("utf-16-le")) > 7000:
        raise ValueError("Shorten the answer so the saved review time fits in one message.")
    for name in references:
        if not draft["automatic"]:
            continue
        record = {
            **records[name],
            "announced_review_at": records[name]["review_at"],
            "announced_at": at.isoformat(),
        }
        key, old_value = rows[name]
        updated = database.execute(
            "UPDATE worker_state SET value=? WHERE key=? AND value=? RETURNING key",
            (json.dumps(record, separators=(",", ":")), key, old_value),
        ).fetchone()
        if not updated:
            raise ValueError("The announced review changed before it could be saved.")
    result["text"] = text
    result["review_schedule"] = {name: records[name]["review_at"] for name in references}
    result.pop("_schedule")
