"""Public, ticker-scoped views of reported SEC activity.

The filing is the unit of pagination. Transaction lines remain separate and joint
filers share one event. Original filings and amendments retain their own sources.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException

from runner_watch.edgar import (
    filing_directory_url,
    parse_beneficial_ownership_xml,
    parse_ownership_xml,
)
from runner_web.db import connection

FORMS = (
    "4",
    "4/A",
    "SC 13D",
    "SC 13D/A",
    "SC 13G",
    "SC 13G/A",
    "SCHEDULE 13D",
    "SCHEDULE 13D/A",
    "SCHEDULE 13G",
    "SCHEDULE 13G/A",
)
ACTIONS = {
    "P": "Bought",
    "S": "Sold",
    "A": "Award or grant",
    "M": "Exercise or conversion",
    "C": "Conversion",
    "F": "Tax or exercise payment",
    "G": "Gift",
    "D": "Disposition to issuer",
    "X": "Exercise",
    "E": "Expiration",
    "H": "Expiration or cancellation",
    "I": "Discretionary transaction",
    "J": "Other transaction",
    "K": "Equity swap",
    "L": "Small acquisition",
    "O": "Out-of-money exercise",
    "U": "Tender disposition",
    "W": "Inheritance",
    "Z": "Trust transfer",
}


def _date(value: Any) -> str | None:
    if not value:
        return None
    text = str(value)
    for pattern in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC).isoformat()
    except ValueError:
        return None


def _source(value: Any) -> str | None:
    url = str(value or "")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.netloc == "www.sec.gov"
            and parsed.path.startswith("/Archives/edgar/data/")
        ):
            return url
    except ValueError:
        pass
    return None


def _person(raw: dict[str, Any], ticker: str) -> dict[str, Any]:
    name = str(raw.get("name") or "Reporting person")
    cik = raw.get("cik")
    identity = (
        f"sec:{int(cik)}"
        if str(cik).isdigit() and int(cik)
        else (
            "name:"
            + hashlib.sha256(f"{ticker}:{name.casefold().strip()}".encode()).hexdigest()[:20]
        )
    )
    from runner_web.wallet_registry import wallet_id_for_person

    return {
        "id": identity,
        # The wallet id is what a link should use; the identity stays for the API.
        "wallet_id": wallet_id_for_person(identity, ticker),
        "name": name,
        "cik": cik,
        "role": raw.get("role") or "Reporting person",
        "identity_basis": "SEC CIK" if identity.startswith("sec:") else "Reported name",
    }


def filing_events(row: dict[str, Any]) -> list[dict[str, Any]]:
    common = {
        "ticker": row["ticker"],
        "company": row.get("company"),
        "accession": row["accession"],
        "filed_at": _date(row["filed_at"]),
        "collected_at": _date(row["created_at"]),
        "source_url": _source(row["filing_url"]),
        "form": row["form"],
        "amendment": row["form"].endswith("/A"),
    }
    payload = json.loads(row.get("evidence_json") or "{}")
    owners = [_person(p, row["ticker"]) for p in payload.get("owners", [])]
    events = []
    for line in payload.get("transactions", []):
        code = line.get("code", "")
        # Codes P/S with an unexpected direction keep their literal filing code.
        action = ACTIONS.get(code, "Reported transaction")
        if code in {"P", "S"} and line.get("direction") != {"P": "A", "S": "D"}[code]:
            action = "Reported transaction"
        events.append(
            {
                **common,
                **line,
                "occurred_at": _date(line.get("occurred_at")),
                "id": f"{row['accession']}:{line['line']}",
                "view": "activity",
                "action": action,
                "people": owners,
                "basis": "Transaction line",
                "tone": "up" if action == "Bought" else "down" if action == "Sold" else "neutral",
                "joint": len(owners) > 1,
            }
        )
    for i, line in enumerate(payload.get("positions", [])):
        events.append(
            {
                **common,
                **line,
                "occurred_at": _date(line.get("occurred_at")),
                "id": f"{row['accession']}:position:{i + 1}",
                "view": "ownership",
                "action": "Reported stake",
                "people": [_person(line, row["ticker"])],
                "basis": "Reporting person and share class",
                "tone": "neutral",
                "joint": False,
            }
        )
    if events:
        return events
    is_stake = "13D" in row["form"] or "13G" in row["form"]
    codes = set(str(row.get("transaction_codes") or "").split(",")) - {""}
    # Legacy rows contain one aggregate side. A mixed filing keeps both action labels.
    action = (
        "Bought and sold"
        if {"P", "S"} <= codes
        else " · ".join(ACTIONS.get(c, c) for c in sorted(codes))
    )
    name = row.get("beneficial_owner_names") if is_stake else row.get("actor")
    people = (
        [
            _person(
                {
                    "name": name,
                    "cik": None if is_stake else row.get("actor_cik"),
                    "role": "Reporting group" if is_stake else row.get("actor_title"),
                },
                row["ticker"],
            )
        ]
        if name
        else []
    )
    return [
        {
            **common,
            "id": f"{row['accession']}:summary",
            "view": "ownership" if is_stake else "activity",
            "action": "Ownership filing" if is_stake else action or "Ownership update",
            "people": people,
            "basis": "Filing summary",
            "tone": "neutral",
            "joint": is_stake,
            "occurred_at": None,
            "security": "",
            "code": ",".join(sorted(codes)),
            "value": row.get("transaction_value") if codes in ({"P"}, {"S"}) else None,
            "shares": row.get("transaction_shares") if codes in ({"P"}, {"S"}) else None,
            "footnotes": "Open the filing for each transaction line and reporting person.",
        }
    ]


def ticker_map(ticker: str, cursor: str | None = None, *, limit: int = 50) -> dict[str, Any]:
    limit = min(50, max(1, limit))
    clause, args = "", []
    if cursor:
        try:
            stamp, accession = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if not isinstance(stamp, str) or not isinstance(accession, str):
                raise ValueError("Invalid map cursor")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid map cursor") from exc
        clause = " AND (filed_at<? OR (filed_at=? AND accession<?))"
        args = [stamp, stamp, accession]
    placeholders = ",".join("?" for _ in FORMS)
    where = f"ticker=? AND form IN ({placeholders})"
    with connection() as db:
        coverage = dict(
            db.execute(
                f"SELECT COUNT(*) AS filings,MIN(filed_at) AS first_filed_at,"
                f"MAX(filed_at) AS last_filed_at FROM sec_filings WHERE {where}",
                (ticker, *FORMS),
            ).fetchone()
        )
        rows = [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM sec_filings WHERE {where}{clause} "
                "ORDER BY filed_at DESC,accession DESC LIMIT ?",
                (ticker, *FORMS, *args, limit + 1),
            ).fetchall()
        ]
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        base64.urlsafe_b64encode(
            json.dumps([rows[-1]["filed_at"], rows[-1]["accession"]]).encode()
        ).decode()
        if has_more
        else None
    )
    return {
        "ticker": ticker,
        "events": [e for row in rows for e in filing_events(row)],
        "loaded_filings": len(rows),
        "coverage": coverage,
        "next_cursor": next_cursor,
    }


def person_connections(
    ticker: str, person_id: str, cursor: str | None = None, *, limit: int = 200
) -> dict[str, Any]:
    """Read a bounded page of candidate filings, then match exact reporting identities.

    The text prefilter includes joint owners and 13D/G cover pages. It only selects
    candidates; parsed CIK equality is required for every cross-company connection.
    Names retain the ticker-scoped identity used by the main map.
    """
    import re

    if not re.fullmatch(r"sec:[1-9][0-9]{0,9}|name:[0-9a-f]{20}", person_id):
        raise ValueError("Invalid person ID")
    limit = min(200, max(1, limit))
    args: list[Any] = list(FORMS)
    where = "form IN (" + ",".join("?" for _ in FORMS) + ")"
    if person_id.startswith("sec:"):
        cik = person_id.removeprefix("sec:")
        where += " AND (actor_cik=? OR evidence_json LIKE ?)"
        args.extend([int(cik), f"%{cik}%"])
    else:
        where += " AND ticker=?"
        args.append(ticker)
    if cursor:
        try:
            stamp, accession = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if not isinstance(stamp, str) or not isinstance(accession, str):
                raise ValueError("Invalid connection cursor")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise ValueError("Invalid connection cursor") from exc
        where += " AND (filed_at<? OR (filed_at=? AND accession<?))"
        args.extend([stamp, stamp, accession])
    with connection() as db:
        rows = [
            dict(row)
            for row in db.execute(
                f"SELECT * FROM sec_filings WHERE {where} "
                "ORDER BY filed_at DESC,accession DESC LIMIT ?",
                (*args, limit + 1),
            ).fetchall()
        ]
    more = len(rows) > limit
    rows = rows[:limit]
    events = []
    for row in rows:
        for event in filing_events(row):
            if any(person["id"] == person_id for person in event["people"]):
                events.append(event)
    return {
        "person_id": person_id,
        "identity_scope": "SEC CIK" if person_id.startswith("sec:") else "This ticker",
        "events": events,
        "next_cursor": base64.urlsafe_b64encode(
            json.dumps([rows[-1]["filed_at"], rows[-1]["accession"]]).encode()
        ).decode()
        if more
        else None,
    }


def restore_archived_map_evidence(limit: int = 20) -> int:
    """Replay a bounded batch of saved XML documents during the collector cycle."""
    restored = 0
    with connection() as db:
        state = db.execute(
            "SELECT value FROM worker_state WHERE key='stock_map_restore_after'"
        ).fetchone()
        after = state["value"] if state else ""
        rows = db.execute(
            "SELECT accession,filing_url,form,cik FROM sec_filings "
            "WHERE evidence_json IS NULL AND accession>? ORDER BY accession LIMIT ?",
            (after, limit),
        ).fetchall()
        for row in rows:
            if row["form"] not in FORMS:
                continue
            try:
                directory = filing_directory_url(row["filing_url"])
            except ValueError:
                continue
            documents = db.execute(
                "SELECT content,content_encoding FROM source_documents "
                "WHERE source='sec' AND source_url LIKE ? ORDER BY first_collected_at LIMIT 20",
                (directory + "/%",),
            ).fetchall()
            for doc in documents:
                try:
                    raw = bytes(doc["content"])
                    if doc["content_encoding"] == "gzip":
                        raw = gzip.decompress(raw)
                    text = raw.decode("utf-8")
                    if row["form"].startswith("4"):
                        summary = parse_ownership_xml(text)
                        if summary.issuer_cik != row["cik"] or not summary.transactions:
                            continue
                        evidence = {
                            "version": 1,
                            "owners": summary.reporting_owners,
                            "transactions": summary.transactions,
                            "positions": [],
                        }
                    else:
                        stake = parse_beneficial_ownership_xml(text)
                        if not stake or not stake.positions or stake.issuer_cik != row["cik"]:
                            continue
                        evidence = {
                            "version": 1,
                            "owners": [],
                            "transactions": [],
                            "positions": stake.positions,
                        }
                except (
                    ValueError,
                    OSError,
                    EOFError,
                    UnicodeError,
                    ParseError,
                    DefusedXmlException,
                ):
                    continue
                db.execute(
                    "UPDATE sec_filings SET evidence_json=? WHERE accession=?",
                    (json.dumps(evidence), row["accession"]),
                )
                restored += 1
                break
        if rows:
            db.execute(
                "INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value,updated_at=excluded.updated_at",
                ("stock_map_restore_after", rows[-1]["accession"], datetime.now(UTC).isoformat()),
            )
    return restored
