from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from runner_watch.edgar import (
    BeneficialOwnershipSummary,
    EdgarClient,
    EdgarFiling,
    OwnershipSummary,
    classify_filing,
)
from runner_watch.models import ScanSettings
from runner_watch.scanner import RunnerScanner
from runner_web.collection import recording_market_data
from runner_web.db import connection
from runner_web.ingestion import mark_source_item, record_source_fetch, source_item_is_terminal
from runner_web.sec_facts import refresh_company_facts

LOG = logging.getLogger(__name__)
INTERESTING_FORMS = (
    "4",
    "8-K",
    "6-K",
    "S-1",
    "S-3",
    "424B",
    "EFFECT",
    "144",
    "SC 13D",
    "SC 13G",
    "NT 10-Q",
    "NT 10-K",
    "10-Q",
    "10-K",
    "20-F",
    "40-F",
)
PARSER_VERSION = "4"


def iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _state(key: str, value: str) -> None:
    with connection() as db:
        db.execute(
            """
            INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
            """,
            (key, value, iso()),
        )


def _company_map_is_fresh() -> bool:
    with connection() as db:
        row = db.execute(
            "SELECT COUNT(*) AS count, MAX(refreshed_at) AS refreshed_at FROM sec_companies"
        ).fetchone()
    if not row or row["count"] < 1 or not row["refreshed_at"]:
        return False
    try:
        return datetime.fromisoformat(row["refreshed_at"]) > datetime.now(UTC) - timedelta(hours=20)
    except ValueError:
        return False


def refresh_company_map(client: EdgarClient) -> int:
    if _company_map_is_fresh():
        with connection() as db:
            return int(db.execute("SELECT COUNT(*) FROM sec_companies").fetchone()[0])
    companies = client.companies()
    refreshed_at = iso()
    with connection() as db:
        db.execute("DELETE FROM sec_companies")
        db.executemany(
            "INSERT INTO sec_companies(cik,ticker,name,exchange,refreshed_at) VALUES(?,?,?,?,?)",
            [
                (company.cik, company.ticker, company.name, company.exchange, refreshed_at)
                for company in companies
            ],
        )
    _state("edgar_company_count", str(len(companies)))
    return len(companies)


def _company_for_cik(cik: int) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            """
            SELECT * FROM sec_companies WHERE cik=?
            ORDER BY CASE exchange WHEN 'Nasdaq' THEN 1 WHEN 'NYSE' THEN 2 ELSE 3 END, ticker
            LIMIT 1
            """,
            (cik,),
        ).fetchone()
    return dict(row) if row else None


def _already_seen(accession: str) -> bool:
    with connection() as db:
        stored = db.execute(
            "SELECT 1 FROM sec_filings WHERE accession=?", (accession,)
        ).fetchone() is not None
    return stored or source_item_is_terminal("sec", "filing", accession)


def _interesting(form: str) -> bool:
    normalized = form.upper()
    return normalized.startswith(INTERESTING_FORMS)


def _ensure_parser_version() -> None:
    """Record the parser version without deleting historical training data."""

    with connection() as db:
        row = db.execute(
            "SELECT value FROM worker_state WHERE key='edgar_parser_version'"
        ).fetchone()
        if row and row["value"] == PARSER_VERSION:
            return
    _state("edgar_parser_version", PARSER_VERSION)


def _market_context(tickers: list[str]) -> dict[str, dict[str, float | None]]:
    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    try:
        result = RunnerScanner(recording_market_data(batch_size=60)).scan(
            unique,
            ScanSettings(
                min_price=0.01,
                max_price=100_000,
                min_avg_volume=0,
                min_avg_dollar_volume=0,
                max_symbols=len(unique),
                top_n=len(unique),
            ),
        )
    except Exception as exc:
        LOG.warning("Market confirmation failed: %s", exc)
        return {}
    return {
        row.ticker: {
            "price": row.price,
            "change_pct": row.change_pct,
            "relative_volume": row.relative_volume,
            "market_score": row.score,
        }
        for row in result.rows
    }


def _prepare_event(
    filing: EdgarFiling,
    company: dict[str, Any],
    ownership: OwnershipSummary | None,
    beneficial: BeneficialOwnershipSummary | None = None,
) -> dict[str, Any]:
    classification = classify_filing(filing.form, ownership)
    ticker = ownership.ticker if ownership and ownership.ticker else company["ticker"]
    is_purchase = bool(ownership and ownership.purchase_value)
    return {
        "accession": filing.accession,
        "cik": ownership.issuer_cik if ownership else filing.cik,
        "ticker": ticker,
        "company": company["name"],
        "form": filing.form,
        "kind": classification["kind"],
        "sentiment": classification["sentiment"],
        "score": float(classification["score"]),
        "title": filing.title,
        "filed_at": filing.filed_at,
        "filing_url": filing.filing_url,
        "actor": ownership.owner_name if ownership else None,
        "actor_title": ownership.owner_title if ownership else None,
        "transaction_codes": ",".join(ownership.codes) if ownership else "",
        "transaction_shares": (
            ownership.purchase_shares if is_purchase else ownership.sale_shares
        )
        if ownership
        else None,
        "transaction_price": (
            ownership.average_purchase_price if is_purchase else ownership.average_sale_price
        )
        if ownership
        else None,
        "transaction_value": (
            ownership.purchase_value if is_purchase else ownership.sale_value
        )
        if ownership
        else None,
        "post_transaction_shares": ownership.post_transaction_shares if ownership else None,
        "stake_change_pct": ownership.stake_change_pct if ownership else None,
        "is_10b5_1": int(ownership.is_10b5_1) if ownership else 0,
        "direct_ownership": (
            int(ownership.direct_ownership)
            if ownership and ownership.direct_ownership is not None
            else None
        ),
        "footnotes": ownership.footnotes if ownership else "",
        "beneficial_ownership_pct": beneficial.ownership_pct if beneficial else None,
        "beneficial_shares": beneficial.beneficial_shares if beneficial else None,
        "beneficial_owner_names": ",".join(beneficial.owner_names) if beneficial else "",
        "reporting_person_types": (
            ",".join(beneficial.reporting_person_types) if beneficial else ""
        ),
    }


def _companyfacts_candidate(new_events: list[dict[str, Any]]) -> int | None:
    if new_events:
        return int(new_events[0]["cik"])
    with connection() as db:
        row = db.execute(
            """
            SELECT c.cik,MAX(f.last_collected_at) AS facts_at
            FROM scan_runs r
            JOIN scan_snapshots s ON s.scan_run_id=r.id
            JOIN sec_companies c ON c.ticker=s.ticker
            LEFT JOIN issuer_facts f ON f.cik=c.cik
            WHERE r.id=(SELECT id FROM scan_runs ORDER BY captured_at DESC LIMIT 1)
            GROUP BY c.cik
            ORDER BY facts_at IS NOT NULL,facts_at,c.cik
            LIMIT 1
            """
        ).fetchone()
    if not row:
        return None
    facts_at = row["facts_at"]
    if facts_at:
        try:
            if datetime.fromisoformat(str(facts_at)) > datetime.now(UTC) - timedelta(hours=20):
                return None
        except ValueError:
            pass
    return int(row["cik"])


def refresh_edgar() -> dict[str, Any]:
    """Fetch the newest filings and persist pre-scored catalyst events."""

    client = EdgarClient(fetch_recorder=record_source_fetch)
    _ensure_parser_version()
    company_count = refresh_company_map(client)
    filings = client.latest_filings()
    new_events: list[dict[str, Any]] = []
    item_errors: dict[str, str] = {}
    for filing in filings:
        if _already_seen(filing.accession):
            continue
        item_payload = {
            "cik": filing.cik,
            "form": filing.form,
            "filed_at": filing.filed_at,
            "filing_url": filing.filing_url,
        }
        if not _interesting(filing.form):
            mark_source_item(
                source="sec",
                feed="filing",
                item_key=filing.accession,
                status="ignored",
                payload=item_payload,
                error="Form is outside the configured filing set",
                parser_version=PARSER_VERSION,
            )
            continue
        ownership: OwnershipSummary | None = None
        beneficial: BeneficialOwnershipSummary | None = None
        issuer_cik = filing.cik
        if filing.form.startswith("4"):
            try:
                ownership = client.ownership_summary(filing)
                if ownership:
                    issuer_cik = ownership.issuer_cik
            except Exception as exc:
                LOG.warning("Could not parse ownership filing %s: %s", filing.accession, exc)
                item_errors[filing.accession] = f"Ownership parsing failed: {exc}"
        elif filing.form.startswith(("SC 13D", "SC 13G")):
            try:
                beneficial = client.beneficial_ownership_summary(filing)
            except Exception as exc:
                LOG.warning("Could not parse beneficial ownership %s: %s", filing.accession, exc)
                item_errors[filing.accession] = f"Beneficial ownership parsing failed: {exc}"
        company = _company_for_cik(issuer_cik)
        if company is None:
            mark_source_item(
                source="sec",
                feed="filing",
                item_key=filing.accession,
                status="ignored",
                payload={**item_payload, "issuer_cik": issuer_cik},
                error="Issuer is not in the listed-company map",
                parser_version=PARSER_VERSION,
            )
            continue
        new_events.append(_prepare_event(filing, company, ownership, beneficial))

    market = _market_context([event["ticker"] for event in new_events])
    timestamp = iso()
    with connection() as db:
        for event in new_events:
            context = market.get(event["ticker"], {})
            market_score = context.get("market_score")
            score = event["score"]
            if market_score is not None:
                score = min(100.0, score + float(market_score) * 0.16)
            price = context.get("price")
            transaction_price = event["transaction_price"]
            transaction_value = event["transaction_value"]
            kind = event["kind"]
            if price and transaction_price:
                price_ratio = float(transaction_price) / float(price)
                if price_ratio < 0.1 or price_ratio > 10:
                    transaction_value = None
                    kind = f"{kind} · filing value needs review"
            if price is not None and float(price) <= 5:
                score = min(100.0, score + 4)
            db.execute(
                """
                INSERT OR IGNORE INTO sec_filings(
                    accession,cik,ticker,company,form,kind,sentiment,score,title,filed_at,
                    filing_url,actor,actor_title,transaction_codes,transaction_shares,
                    transaction_price,transaction_value,price,change_pct,relative_volume,
                    market_score,created_at,updated_at,parser_version,
                    post_transaction_shares,stake_change_pct,is_10b5_1,
                    direct_ownership,footnotes
                    ,beneficial_ownership_pct,beneficial_shares,beneficial_owner_names,
                    reporting_person_types
                ) VALUES(
                    :accession,:cik,:ticker,:company,:form,:kind,:sentiment,:score,:title,
                    :filed_at,:filing_url,:actor,:actor_title,:transaction_codes,
                    :transaction_shares,:transaction_price,:transaction_value,:price,
                    :change_pct,:relative_volume,:market_score,:created_at,:updated_at,
                    :parser_version,:post_transaction_shares,:stake_change_pct,
                    :is_10b5_1,:direct_ownership,:footnotes
                    ,:beneficial_ownership_pct,:beneficial_shares,:beneficial_owner_names,
                    :reporting_person_types
                )
                """,
                {
                    **event,
                    "kind": kind,
                    "score": round(score, 1),
                    "transaction_value": transaction_value,
                    "price": price,
                    "change_pct": context.get("change_pct"),
                    "relative_volume": context.get("relative_volume"),
                    "market_score": market_score,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                    "parser_version": PARSER_VERSION,
                },
            )

    for event in new_events:
        context = market.get(event["ticker"], {})
        errors = [item_errors[event["accession"]]] if event["accession"] in item_errors else []
        if not context:
            errors.append("Yahoo market context was unavailable")
        mark_source_item(
            source="sec",
            feed="filing",
            item_key=event["accession"],
            status="processed",
            payload={
                "cik": event["cik"],
                "ticker": event["ticker"],
                "form": event["form"],
                "filed_at": event["filed_at"],
                "filing_url": event["filing_url"],
            },
            error="; ".join(errors) or None,
            parser_version=PARSER_VERSION,
        )

    fact_result: dict[str, Any] = {"facts": 0}
    fact_cik = _companyfacts_candidate(new_events)
    if fact_cik is not None:
        try:
            fact_result = refresh_company_facts(fact_cik)
            _state("edgar_companyfacts_last_error", "")
        except Exception as exc:
            LOG.warning("Could not refresh SEC company facts for CIK %s: %s", fact_cik, exc)
            _state("edgar_companyfacts_last_error", str(exc)[:500])

    _state("edgar_last_refresh", timestamp)
    _state("edgar_last_error", "")
    _state("edgar_last_new_events", str(len(new_events)))
    return {
        "companies": company_count,
        "feed_filings": len(filings),
        "new_events": len(new_events),
        "issuer_facts": int(fact_result.get("facts") or 0),
        "refreshed_at": timestamp,
    }


def record_edgar_error(exc: Exception) -> None:
    LOG.exception("EDGAR background refresh failed")
    _state("edgar_last_error", str(exc)[:500])
