from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from runner_web.db import connection

MONITOR_VERSION = "case-monitor-v1"
RISK_EVENT_TYPES = {"trading_halt", "reverse_split", "corporate_action", "security_action"}
STOP_WORDS = {
    "a",
    "an",
    "and",
    "at",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "the",
    "to",
    "with",
    "stock",
    "shares",
    "company",
    "announces",
    "reports",
}
ACCESSION_RE = re.compile(r"\b\d{10}-?\d{2}-?\d{6}\b")


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _row(row: Any) -> dict[str, Any]:
    return {str(key): row[key] for key in row.keys()}


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _tokens(value: str, ticker: str = "") -> set[str]:
    words = re.findall(r"[a-z0-9]+", value.lower())
    blocked = STOP_WORDS | {ticker.lower()}
    return {word for word in words if len(word) > 2 and word not in blocked}


def _similar_headlines(left: str, right: str, ticker: str) -> bool:
    left_tokens = _tokens(left, ticker)
    right_tokens = _tokens(right, ticker)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    sequence = SequenceMatcher(None, left.lower(), right.lower()).ratio()
    return (len(left_tokens & right_tokens) >= 3 and overlap >= 0.55) or sequence >= 0.82


def _accession_in(value: str, filings: list[dict[str, Any]]) -> str | None:
    flattened = re.sub(r"\D", "", value)
    for match in ACCESSION_RE.findall(value):
        candidate = re.sub(r"\D", "", match)
        for filing in filings:
            accession = str(filing.get("accession") or "")
            if re.sub(r"\D", "", accession) == candidate:
                return accession
    for filing in filings:
        accession = str(filing.get("accession") or "")
        digits = re.sub(r"\D", "", accession)
        if len(digits) == 18 and digits in flattened:
            return accession
    return None


def _news_filing_match(
    title: str,
    event_at: str,
    filings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    lowered = title.lower()
    families: list[str] = []
    if any(term in lowered for term in ("offering", "registration", "dilution", "prospectus")):
        families.append("offering")
    if any(term in lowered for term in ("insider", "director", "officer", "purchase", "sold")):
        families.append("ownership")
    if any(term in lowered for term in ("stake", "beneficial owner", "13d", "13g")):
        families.append("stake")
    if not families:
        return None
    news_time = _time(event_at)
    matches: list[tuple[float, dict[str, Any]]] = []
    for filing in filings:
        filing_time = _time(filing.get("filed_at"))
        if not news_time or not filing_time:
            continue
        distance = abs((news_time - filing_time).total_seconds())
        if distance > 36 * 3600:
            continue
        form = str(filing.get("form") or "").upper()
        kind = str(filing.get("kind") or "").lower()
        matches_family = (
            ("offering" in families and ("offering" in kind or form.startswith(("S-", "424B"))))
            or ("ownership" in families and form == "4")
            or ("stake" in families and form.startswith(("SC 13", "13")))
        )
        if matches_family:
            matches.append((distance, filing))
    return min(matches, key=lambda item: item[0])[1] if matches else None


def _filing_direction(filing: dict[str, Any]) -> str:
    sentiment = str(filing.get("sentiment") or "").lower()
    if sentiment == "risk":
        return "risks"
    if sentiment == "positive":
        return "supports"
    return "neutral"


def _upsert_claim(
    database: Any,
    *,
    ticker: str,
    claim_key: str,
    claim_type: str,
    summary: str,
    direction: str,
    primary_source_type: str,
    primary_source_url: str | None,
    occurred_at: str,
    collected_at: str,
    payload: dict[str, Any],
    source_key: str,
    source_type: str,
    source_url: str | None,
    source_title: str,
    source_payload: dict[str, Any],
) -> dict[str, Any]:
    timestamp = _iso()
    existing_row = database.execute(
        "SELECT * FROM evidence_claims WHERE ticker=? AND claim_key=?",
        (ticker, claim_key),
    ).fetchone()
    if not existing_row:
        claim_id = str(uuid.uuid4())
        database.execute(
            """
            INSERT INTO evidence_claims(
                id,ticker,claim_key,claim_type,summary,direction,primary_source_type,
                primary_source_url,occurred_at,first_collected_at,last_collected_at,
                source_count,payload_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                claim_id,
                ticker,
                claim_key,
                claim_type,
                summary[:500],
                direction,
                primary_source_type,
                primary_source_url,
                occurred_at,
                collected_at,
                collected_at,
                1,
                json.dumps(payload, separators=(",", ":")),
                timestamp,
                timestamp,
            ),
        )
    else:
        existing = _row(existing_row)
        claim_id = str(existing["id"])
        promote_primary = (
            primary_source_type == "sec" and existing["primary_source_type"] != "sec"
        )
        database.execute(
            """
            UPDATE evidence_claims SET
                summary=?,direction=?,primary_source_type=?,primary_source_url=?,
                occurred_at=?,last_collected_at=?,payload_json=?,updated_at=?
            WHERE id=?
            """,
            (
                summary[:500] if promote_primary else existing["summary"],
                direction if promote_primary else existing["direction"],
                primary_source_type if promote_primary else existing["primary_source_type"],
                primary_source_url if promote_primary else existing["primary_source_url"],
                min(str(existing["occurred_at"]), occurred_at),
                max(str(existing["last_collected_at"]), collected_at),
                json.dumps(payload, separators=(",", ":"))
                if promote_primary
                else existing["payload_json"],
                timestamp,
                claim_id,
            ),
        )
    database.execute(
        """
        INSERT OR IGNORE INTO evidence_claim_sources(
            claim_id,source_key,source_type,source_url,title,occurred_at,
            collected_at,payload_json
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            claim_id,
            source_key,
            source_type,
            source_url,
            source_title[:500],
            occurred_at,
            collected_at,
            json.dumps(source_payload, separators=(",", ":")),
        ),
    )
    source_count = int(
        database.execute(
            "SELECT COUNT(*) FROM evidence_claim_sources WHERE claim_id=?",
            (claim_id,),
        ).fetchone()[0]
    )
    database.execute(
        "UPDATE evidence_claims SET source_count=?,updated_at=? WHERE id=?",
        (source_count, timestamp, claim_id),
    )
    refreshed = database.execute("SELECT * FROM evidence_claims WHERE id=?", (claim_id,)).fetchone()
    assert refreshed is not None
    return _row(refreshed)


def _materialize_claims(
    database: Any,
    tickers: list[str],
    cutoff: str,
) -> tuple[int, dict[str, dict[str, Any]]]:
    placeholders = ",".join("?" for _ in tickers)
    filings = [
        _row(row)
        for row in database.execute(
            f"""
            SELECT * FROM sec_filings
            WHERE ticker IN ({placeholders}) AND created_at>=?
            ORDER BY filed_at
            """,
            (*tickers, cutoff),
        ).fetchall()
    ]
    events = [
        _row(row)
        for row in database.execute(
            f"""
            SELECT * FROM market_events
            WHERE ticker IN ({placeholders}) AND first_collected_at>=?
            ORDER BY event_at,last_collected_at
            """,
            (*tickers, cutoff),
        ).fetchall()
    ]
    snapshots = [
        _row(row)
        for row in database.execute(
            f"""
            WITH ranked AS (
                SELECT s.*,ROW_NUMBER() OVER (
                    PARTITION BY ticker ORDER BY captured_at DESC
                ) AS monitor_position
                FROM scan_snapshots s WHERE ticker IN ({placeholders})
            )
            SELECT * FROM ranked WHERE monitor_position<=2
            ORDER BY ticker,captured_at DESC
            """,
            tickers,
        ).fetchall()
    ]
    filings_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for filing in filings:
        filings_by_ticker.setdefault(str(filing["ticker"]), []).append(filing)
    existing_news = [
        _row(row)
        for row in database.execute(
            f"""
            SELECT * FROM evidence_claims
            WHERE ticker IN ({placeholders}) AND claim_type='news' AND occurred_at>=?
            """,
            (*tickers, cutoff),
        ).fetchall()
    ]
    touched: set[str] = set()
    for filing in filings:
        ticker = str(filing["ticker"])
        accession = str(filing["accession"])
        summary = str(filing.get("title") or f"{filing.get('form') or 'SEC'} filing")
        claim = _upsert_claim(
            database,
            ticker=ticker,
            claim_key=f"sec:{accession}",
            claim_type="filing",
            summary=summary,
            direction=_filing_direction(filing),
            primary_source_type="sec",
            primary_source_url=filing.get("filing_url"),
            occurred_at=str(filing["filed_at"]),
            collected_at=str(filing["created_at"]),
            payload={
                "accession": accession,
                "form": filing.get("form"),
                "kind": filing.get("kind"),
                "sentiment": filing.get("sentiment"),
                "score": filing.get("score"),
            },
            source_key=f"sec:{accession}",
            source_type="sec",
            source_url=filing.get("filing_url"),
            source_title=summary,
            source_payload={"accession": accession, "form": filing.get("form")},
        )
        touched.add(str(claim["id"]))
    for event in events:
        ticker = str(event["ticker"])
        event_type = str(event.get("event_type") or "event")
        payload = _json(event.get("payload_json"), {})
        title = str(payload.get("title") or event_type.replace("_", " ").title())
        source_key = ":".join(
            (
                "event",
                str(event.get("source") or "unknown"),
                str(event.get("feed") or "unknown"),
                str(event.get("event_id") or "unknown"),
                str(event.get("version") or "unknown"),
            )
        )
        direction = "risks" if event_type in RISK_EVENT_TYPES else "neutral"
        claim_type = event_type
        claim_key = (
            f"event:{event_type}:{event.get('source')}:"
            f"{event.get('event_id')}:{event.get('status')}"
        )
        primary_type = str(event.get("source") or "event")
        primary_url = event.get("source_url")
        summary = title
        if event_type == "news_article":
            filing_rows = filings_by_ticker.get(ticker, [])
            accession = _accession_in(
                f"{title} {event.get('source_url') or ''} {json.dumps(payload)}",
                filing_rows,
            )
            matched_filing = (
                next((row for row in filing_rows if row["accession"] == accession), None)
                if accession
                else _news_filing_match(title, str(event["event_at"]), filing_rows)
            )
            if matched_filing:
                claim_key = f"sec:{matched_filing['accession']}"
                claim_type = "filing"
            else:
                matching_claim = next(
                    (
                        claim
                        for claim in existing_news
                        if claim["ticker"] == ticker
                        and abs(
                            (
                                (_time(claim["occurred_at"]) or datetime.min.replace(tzinfo=UTC))
                                - (_time(event["event_at"]) or datetime.max.replace(tzinfo=UTC))
                            ).total_seconds()
                        )
                        <= 48 * 3600
                        and _similar_headlines(str(claim["summary"]), title, ticker)
                    ),
                    None,
                )
                if matching_claim:
                    claim_key = str(matching_claim["claim_key"])
                else:
                    token_key = " ".join(sorted(_tokens(title, ticker))) or title.lower()
                    digest = hashlib.sha256(token_key.encode()).hexdigest()[:20]
                    day = str(event["event_at"])[:10]
                    claim_key = f"news:{day}:{digest}"
                    existing_news.append(
                        {
                            "ticker": ticker,
                            "claim_key": claim_key,
                            "summary": title,
                            "occurred_at": event["event_at"],
                        }
                    )
                claim_type = "news"
                primary_type = "news"
        elif event_type == "social_spike":
            claim_type = "social"
            claim_key = f"social:{event.get('source')}:{event.get('event_id')}"
            network = str(payload.get("network_label") or "Social")
            mentions = int(payload.get("mention_count") or 0)
            summary = f"{network} activity reached {mentions} mentions"
        elif event_type == "trading_halt":
            summary = f"Trading halt status: {event.get('status') or 'unknown'}"
        claim = _upsert_claim(
            database,
            ticker=ticker,
            claim_key=claim_key,
            claim_type=claim_type,
            summary=summary,
            direction=direction,
            primary_source_type=primary_type,
            primary_source_url=primary_url,
            occurred_at=str(event["event_at"]),
            collected_at=str(event["first_collected_at"]),
            payload=payload,
            source_key=source_key,
            source_type=primary_type,
            source_url=event.get("source_url"),
            source_title=title,
            source_payload=payload,
        )
        touched.add(str(claim["id"]))
    snapshots_by_ticker: dict[str, list[dict[str, Any]]] = {}
    for snapshot in snapshots:
        snapshots_by_ticker.setdefault(str(snapshot["ticker"]), []).append(snapshot)
    latest_risk: dict[str, dict[str, Any]] = {}
    for ticker, rows in snapshots_by_ticker.items():
        latest = rows[0]
        prior = rows[1] if len(rows) > 1 else None
        latest_risk[ticker] = latest
        state = str(latest.get("trade_state") or "WATCH").upper()
        rug_level = str(latest.get("rug_level") or "unknown").lower()
        hard_veto = bool(latest.get("hard_veto"))
        changed = not prior or any(
            latest.get(field) != prior.get(field)
            for field in ("trade_state", "rug_level", "hard_veto")
        )
        risk_active = hard_veto or state in {"AVOID", "EXIT"} or rug_level in {"high", "critical"}
        if not changed and not risk_active:
            continue
        direction = (
            "risks"
            if risk_active
            else "supports"
            if state in {"ARMED", "TRIGGERED"}
            else "neutral"
        )
        reason = str(latest.get("state_reason") or "").strip()
        summary = (
            f"Risk veto active: {reason or state}"
            if risk_active
            else f"Deterministic trade state changed to {state}"
        )
        claim = _upsert_claim(
            database,
            ticker=ticker,
            claim_key=(
                f"market-risk:{latest['id']}:{state}:{rug_level}:{int(hard_veto)}"
            ),
            claim_type="market_risk",
            summary=summary,
            direction=direction,
            primary_source_type="stored_market_state",
            primary_source_url=f"/t/{ticker}",
            occurred_at=str(latest["captured_at"]),
            collected_at=str(latest["captured_at"]),
            payload={
                "snapshot_id": latest["id"],
                "trade_state": state,
                "rug_score": latest.get("rug_score"),
                "rug_level": rug_level,
                "hard_veto": hard_veto,
                "state_reason": reason,
            },
            source_key=f"snapshot:{latest['id']}",
            source_type="stored_market_state",
            source_url=f"/t/{ticker}",
            source_title=summary,
            source_payload={"snapshot_id": latest["id"]},
        )
        touched.add(str(claim["id"]))
    return len(touched), latest_risk


def _veto_receipt(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {"available": False, "hard_veto": False}
    return {
        "available": True,
        "snapshot_id": snapshot.get("id"),
        "captured_at": snapshot.get("captured_at"),
        "trade_state": snapshot.get("trade_state"),
        "rug_score": snapshot.get("rug_score"),
        "rug_level": snapshot.get("rug_level"),
        "hard_veto": bool(snapshot.get("hard_veto")),
        "state_reason": snapshot.get("state_reason"),
    }


def _citations(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": claim["id"],
            "label": claim["summary"],
            "url": claim.get("primary_source_url"),
            "source_type": claim["primary_source_type"],
            "occurred_at": claim["occurred_at"],
            "source_count": int(claim.get("source_count") or 1),
        }
        for claim in claims[:5]
    ]


def refresh_case_monitor(at: datetime | None = None) -> dict[str, int]:
    timestamp = _iso(at)
    with connection() as database:
        cases = [
            _row(row)
            for row in database.execute(
                "SELECT * FROM thesis_cases WHERE status='active' ORDER BY created_at"
            ).fetchall()
        ]
        if not cases:
            return {"active_cases": 0, "claims_touched": 0, "claims_linked": 0, "updates": 0}
        tickers = list(dict.fromkeys(str(case["ticker"]) for case in cases))
        earliest = min(_time(case["created_at"]) or datetime.now(UTC) for case in cases)
        cutoff = _iso(max(earliest - timedelta(days=2), datetime.now(UTC) - timedelta(days=365)))
        claims_touched, latest_risk = _materialize_claims(database, tickers, cutoff)
        claims_linked = 0
        updates = 0
        for case in cases:
            case_id = str(case["id"])
            unlinked = [
                _row(row)
                for row in database.execute(
                    """
                    SELECT e.* FROM evidence_claims e
                    LEFT JOIN thesis_case_claims link
                      ON link.case_id=? AND link.claim_id=e.id
                    WHERE e.ticker=? AND link.claim_id IS NULL
                      AND (
                        e.first_collected_at>=?
                        OR (e.claim_type='market_risk' AND e.direction='risks')
                      )
                    ORDER BY
                      CASE e.direction WHEN 'risks' THEN 0 WHEN 'supports' THEN 1 ELSE 2 END,
                      e.occurred_at DESC
                    """,
                    (case_id, case["ticker"], case["created_at"]),
                ).fetchall()
            ]
            if not unlinked:
                continue
            database.executemany(
                """
                INSERT OR IGNORE INTO thesis_case_claims(case_id,claim_id,linked_at)
                VALUES(?,?,?)
                """,
                [(case_id, claim["id"], timestamp) for claim in unlinked],
            )
            claims_linked += len(unlinked)
            material = [claim for claim in unlinked if claim["direction"] != "neutral"]
            if not material:
                continue
            veto = _veto_receipt(latest_risk.get(str(case["ticker"])))
            has_risk = veto["hard_veto"] or any(claim["direction"] == "risks" for claim in material)
            direction = "weakened" if has_risk else "strengthened"
            first = material[0]
            extra = len(material) - 1
            summary = f"Thesis {direction}: {str(first['summary']).rstrip('.')}"
            if extra:
                summary += f". {extra} other new evidence group{'s' if extra != 1 else ''} reviewed"
            summary += "."
            if veto["hard_veto"]:
                action = "Risk veto active. Review before acting."
            elif direction == "weakened":
                action = "Review the view and its risks before acting."
            else:
                action = "Evidence improved, but keep the same risk limits."
            fingerprint_payload = {
                "version": MONITOR_VERSION,
                "case_id": case_id,
                "claim_ids": sorted(str(claim["id"]) for claim in material),
                "veto": veto,
            }
            fingerprint = hashlib.sha256(
                json.dumps(fingerprint_payload, sort_keys=True).encode()
            ).hexdigest()
            before = float(case["confidence"]) if case.get("confidence") is not None else None
            cursor = database.execute(
                """
                INSERT OR IGNORE INTO thesis_case_updates(
                    id,case_id,kind,direction,summary,recommended_action,
                    confidence_before,confidence_after,citations_json,evidence_fingerprint,
                    deterministic_veto_json,model_provider,model_name,model_version,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(uuid.uuid4()),
                    case_id,
                    "evidence_change",
                    direction,
                    summary[:1000],
                    action,
                    before,
                    before,
                    json.dumps(_citations(material), separators=(",", ":")),
                    fingerprint,
                    json.dumps(veto, separators=(",", ":")),
                    None,
                    None,
                    MONITOR_VERSION,
                    timestamp,
                ),
            )
            if cursor.rowcount:
                updates += 1
        return {
            "active_cases": len(cases),
            "claims_touched": claims_touched,
            "claims_linked": claims_linked,
            "updates": updates,
        }
