"""A record of every Ratified result, with the rules version and the facts used.

A result is recomputed on each read, so it is recorded when it changes: a new
rules version, a change in whether it is Ratified, or any standard changing
state. Each record keeps the rules version and digest, the result, and the
facts the rules read, so a past result can be reproduced under the rules then
in force. Details that move without a change of state (a pool's age in hours)
do not make a new record.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from functools import cache
from importlib.resources import files
from typing import Any

import ratitrust

LOG = logging.getLogger(__name__)
SUFFIXES = (".py", ".toml", ".json")


@cache
def rules_digest() -> str:
    """The digest of the rules this service runs, as trust.rati.chat publishes it."""

    package = files("ratitrust")
    names = sorted(
        entry.name
        for entry in package.iterdir()
        if entry.is_file() and entry.name.endswith(SUFFIXES)
    )
    manifest = "".join(
        f"src/ratitrust/{name} {hashlib.sha256(package.joinpath(name).read_bytes()).hexdigest()}\n"
        for name in names
    )
    return hashlib.sha256(manifest.encode()).hexdigest()


def _json(value: Any) -> str:
    """Dates as ISO text and sets as sorted lists, so `ratitrust.reproduce` reads them."""

    return json.dumps(
        value,
        default=lambda item: sorted(item) if isinstance(item, set | frozenset) else str(item),
        sort_keys=True,
    )


def fingerprint(result: dict[str, Any]) -> str:
    state = [
        ratitrust.__version__,
        bool(result.get("ratified")),
        [(item["key"], item["met"], item["applies"]) for item in result.get("standards") or []],
    ]
    return hashlib.sha256(json.dumps(state).encode()).hexdigest()


def record_results(
    database: Any,
    market: str,
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    at: datetime,
) -> int:
    """Record each subject's (result, facts) whose state changed; returns how many."""

    if not results:
        return 0
    subjects = list(results)
    latest: dict[str, str] = {}
    for offset in range(0, len(subjects), 500):
        chunk = subjects[offset : offset + 500]
        marks = ",".join("?" for _ in chunk)
        for row in database.execute(
            f"SELECT subject,fingerprint FROM ratification_latest "
            f"WHERE market=? AND subject IN ({marks})",
            (market, *chunk),
        ).fetchall():
            latest[row["subject"]] = row["fingerprint"]
    recorded = 0
    for subject, (result, facts) in results.items():
        mark = fingerprint(result)
        if latest.get(subject) == mark:
            continue
        database.execute(
            """
            INSERT INTO ratification_records(
                market,subject,rules_version,rules_digest,ratified,met,total,
                result_json,facts_json,recorded_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                market,
                subject,
                ratitrust.__version__,
                rules_digest(),
                1 if result.get("ratified") else 0,
                int(result.get("met") or 0),
                int(result.get("total") or 0),
                _json(result),
                _json(facts),
                at.isoformat(),
            ),
        )
        database.execute(
            """
            INSERT INTO ratification_latest(market,subject,fingerprint,recorded_at)
            VALUES(?,?,?,?)
            ON CONFLICT(market,subject) DO UPDATE SET
                fingerprint=excluded.fingerprint,recorded_at=excluded.recorded_at
            """,
            (market, subject, mark, at.isoformat()),
        )
        recorded += 1
    return recorded


def safely_record(
    market: str,
    results: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    at: datetime,
) -> None:
    """Record in a transaction of its own; a failed write never takes a result off a page."""

    from runner_web.db import connection

    try:
        with connection() as database:
            record_results(database, market, results, at=at)
    except Exception:
        LOG.warning("Ratification records failed for %s", market, exc_info=True)


COLUMNS = "id,rules_version,rules_digest,ratified,met,total,result_json,facts_json,recorded_at"


def _item(row: Any) -> dict[str, Any]:
    return {
        **{key: row[key] for key in ("id", "rules_version", "rules_digest", "met", "total")},
        "ratified": bool(row["ratified"]),
        "result": json.loads(row["result_json"]),
        "facts": json.loads(row["facts_json"]),
        "recorded_at": row["recorded_at"],
    }


def history(database: Any, market: str, subject: str, limit: int = 50) -> list[dict[str, Any]]:
    rows = database.execute(
        f"SELECT {COLUMNS} FROM ratification_records WHERE market=? AND subject=? "
        "ORDER BY recorded_at DESC, id DESC LIMIT ?",
        (market, subject, limit),
    ).fetchall()
    return [_item(row) for row in rows]


def record(database: Any, market: str, subject: str, record_id: int) -> dict[str, Any] | None:
    """One record in full: what anyone needs to recompute it with `ratitrust.reproduce`."""

    row = database.execute(
        f"SELECT {COLUMNS} FROM ratification_records WHERE id=? AND market=? AND subject=?",
        (record_id, market, subject),
    ).fetchone()
    return _item(row) if row else None


def _label(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value
    return f"{moment:%b} {moment.day}, {moment:%H:%M} UTC"


def page_history(market: str, subject: str, limit: int = 6) -> list[dict[str, Any]]:
    """The latest changes for an asset page, newest first, without their facts.

    A page reads this in its own transaction; a failed read shows no history.
    """

    from runner_web.db import connection

    try:
        with connection() as database:
            items = history(database, market, subject, limit=limit)
    except Exception:
        LOG.warning("Ratification history unavailable for %s %s", market, subject, exc_info=True)
        return []
    return [
        {
            "id": item["id"],
            "ratified": item["ratified"],
            "met": item["met"],
            "total": item["total"],
            "unchecked": item["result"].get("unchecked", 0),
            "rules_version": item["rules_version"],
            "rules_digest": item["rules_digest"],
            "recorded_at": item["recorded_at"],
            "recorded_label": _label(item["recorded_at"]),
            "url": f"/api/ratification/{market}/{subject}/{item['id']}.json",
        }
        for item in items
    ]
