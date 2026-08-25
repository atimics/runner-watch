from __future__ import annotations

import gzip
import hashlib
import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from runner_watch.ingestion import (
    EntityLink,
    IssuerFact,
    MacroObservation,
    MarketEvent,
    SecurityQuote,
    SourceBatch,
    SourceFetch,
)
from runner_web.db import connection
from runner_web.source_catalog import SourcePolicy

TERMINAL_ITEM_STATUSES = {"processed", "ignored", "rejected"}


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _column(frame: Any, name: str) -> Any:
    for column in frame.columns:
        if str(column).lower().replace(" ", "") == name.lower():
            return frame[column]
    import pandas as pd

    return pd.Series(index=frame.index, dtype="float64")


def _requested_count(fetch: SourceFetch) -> int:
    requested_count = fetch.metadata.get("requested_count")
    if isinstance(requested_count, int) and requested_count >= 0:
        return requested_count
    requested = fetch.metadata.get("requested_tickers")
    if isinstance(requested, list):
        return len(requested)
    if fetch.source == "sec":
        return 1
    size = fetch.metadata.get("size")
    return int(size) if isinstance(size, int) and size >= 0 else 0


def _received_count(fetch: SourceFetch) -> int:
    if fetch.status == "error":
        return 0
    received_count = fetch.metadata.get("received_count")
    if isinstance(received_count, int) and received_count >= 0:
        return received_count
    if fetch.feed == "market_bars" and isinstance(fetch.payload, dict):
        return len(fetch.payload)
    if fetch.feed == "universe" and isinstance(fetch.payload, list):
        return len(fetch.payload)
    return 1 if fetch.payload is not None else 0


def _batch_size(batch: SourceBatch) -> int:
    return sum(
        len(records)
        for records in (
            batch.market_events,
            batch.security_quotes,
            batch.issuer_facts,
            batch.entity_links,
            batch.macro_observations,
        )
    )


def _write_source_policy(database: Any, policy: SourcePolicy, timestamp: str) -> None:
    database.execute(
        """
        INSERT INTO source_registry(
            source,feed,title,owner,terms_url,credential_env,
            expected_cadence_seconds,stale_after_seconds,schedule,
            storage_policy,display_policy,attribution,review_status,enabled,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source,feed) DO UPDATE SET
            title=excluded.title,owner=excluded.owner,terms_url=excluded.terms_url,
            credential_env=excluded.credential_env,
            expected_cadence_seconds=excluded.expected_cadence_seconds,
            stale_after_seconds=excluded.stale_after_seconds,schedule=excluded.schedule,
            storage_policy=excluded.storage_policy,display_policy=excluded.display_policy,
            attribution=excluded.attribution,review_status=excluded.review_status,
            enabled=excluded.enabled,updated_at=excluded.updated_at
        """,
        (
            policy.source,
            policy.feed,
            policy.title,
            policy.owner,
            policy.terms_url,
            policy.credential_env,
            policy.expected_cadence_seconds,
            policy.stale_after_seconds,
            policy.schedule,
            policy.storage_policy,
            policy.display_policy,
            policy.attribution,
            policy.review_status,
            int(policy.enabled),
            timestamp,
            timestamp,
        ),
    )


def register_source(policy: SourcePolicy) -> None:
    """Create or update one feed's written access and freshness rules."""

    with connection() as database:
        _write_source_policy(database, policy, _iso())


def _ensure_source_policy(database: Any, fetch: SourceFetch, timestamp: str) -> None:
    row = database.execute(
        "SELECT 1 FROM source_registry WHERE source=? AND feed=?",
        (fetch.source, fetch.feed),
    ).fetchone()
    if row:
        return
    _write_source_policy(
        database,
        SourcePolicy(
            source=fetch.source,
            feed=fetch.feed,
            title=f"{fetch.source} {fetch.feed}".replace("_", " ").title(),
            owner=fetch.source,
            terms_url=None,
            credential_env=None,
            expected_cadence_seconds=None,
            stale_after_seconds=None,
            schedule="event",
            storage_policy="review_required",
            display_policy="review_required",
            attribution=None,
            review_status="review_required",
        ),
        timestamp,
    )


def _archive_document(database: Any, fetch: SourceFetch, digest: str, collected_at: str) -> None:
    body = fetch.payload
    if not isinstance(body, bytes):
        return
    if len(body) > 512:
        stored_body = gzip.compress(body, compresslevel=6, mtime=0)
        content_encoding = "gzip"
    else:
        stored_body = body
        content_encoding = "identity"
    database.execute(
        """
        INSERT INTO source_documents(
            source,source_url,content_hash,content_type,content_encoding,content,
            first_collected_at,last_collected_at
        ) VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(source_url,content_hash) DO UPDATE SET
            last_collected_at=excluded.last_collected_at
        """,
        (
            fetch.source,
            fetch.locator,
            digest,
            fetch.content_type or "application/octet-stream",
            content_encoding,
            stored_body,
            collected_at,
            collected_at,
        ),
    )


def _market_projection(
    database: Any, run_id: str, fetch: SourceFetch, collected_at: str
) -> str:
    import pandas as pd

    frames = fetch.payload
    if not isinstance(frames, dict):
        raise TypeError("Yahoo market payload must be a ticker-to-frame mapping")
    interval = str(fetch.metadata.get("interval") or "unknown")
    rows: list[tuple[Any, ...]] = []
    digest = hashlib.sha256()
    found: set[str] = set()
    for ticker, frame in sorted(frames.items()):
        if not isinstance(frame, pd.DataFrame) or frame.empty:
            continue
        symbol = str(ticker).upper()
        found.add(symbol)
        columns = {
            name: _column(frame, name) for name in ("Open", "High", "Low", "Close", "Volume")
        }
        first_bar: str | None = None
        last_bar: str | None = None
        bar_count = 0
        for index in frame.index:
            bar_time = pd.Timestamp(index).isoformat()
            values = (
                "yahoo",
                symbol,
                interval,
                bar_time,
                _number(columns["Open"].get(index)),
                _number(columns["High"].get(index)),
                _number(columns["Low"].get(index)),
                _number(columns["Close"].get(index)),
                _number(columns["Volume"].get(index)),
                collected_at,
                collected_at,
            )
            rows.append(values)
            digest.update(_json(values[:9]).encode())
            first_bar = first_bar or bar_time
            last_bar = bar_time
            bar_count += 1
        database.execute(
            """
            INSERT INTO ingestion_items(run_id,item_key,status,payload_json)
            VALUES(?,?,?,?)
            """,
            (
                run_id,
                symbol,
                "accepted",
                _json(
                    {
                        "interval": interval,
                        "bar_count": bar_count,
                        "first_bar": first_bar,
                        "last_bar": last_bar,
                    }
                ),
            ),
        )
    for ticker in fetch.metadata.get("missing_tickers", []):
        symbol = str(ticker).upper()
        if symbol in found:
            continue
        database.execute(
            """
            INSERT INTO ingestion_items(run_id,item_key,status,payload_json,error)
            VALUES(?,?,?,?,?)
            """,
            (run_id, symbol, "missing", "{}", "Yahoo returned no usable frame"),
        )
    if rows:
        database.executemany(
            """
            INSERT INTO market_bars(
                source,ticker,interval,bar_time,open,high,low,close,volume,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,ticker,interval,bar_time) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,
                close=excluded.close,volume=excluded.volume,
                last_collected_at=excluded.last_collected_at
            """,
            rows,
        )
    return digest.hexdigest()


def _universe_projection(database: Any, run_id: str, fetch: SourceFetch) -> str:
    quotes = fetch.payload
    if not isinstance(quotes, list):
        raise TypeError("Yahoo universe payload must be a list")
    digest = hashlib.sha256(_json(quotes).encode()).hexdigest()
    for index, quote in enumerate(quotes):
        payload = quote if isinstance(quote, dict) else {"value": quote}
        symbol = str(payload.get("symbol") or f"row-{index}").strip().upper()
        database.execute(
            """
            INSERT OR REPLACE INTO ingestion_items(
                run_id,item_key,status,payload_json,error
            ) VALUES(?,?,?,?,?)
            """,
            (
                run_id,
                symbol,
                "accepted" if payload.get("symbol") else "rejected",
                _json(payload),
                None if payload.get("symbol") else "Universe row has no symbol",
            ),
        )
    return digest


def _stable_id(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _record_batch_item(
    database: Any,
    run_id: str,
    item_key: str,
    payload: dict[str, Any],
) -> None:
    database.execute(
        """
        INSERT OR REPLACE INTO ingestion_items(run_id,item_key,status,payload_json,error)
        VALUES(?,?,?,?,NULL)
        """,
        (run_id, item_key, "accepted", _json(payload)),
    )


def _market_event_projection(
    database: Any,
    run_id: str,
    fetch: SourceFetch,
    events: tuple[MarketEvent, ...],
    collected_at: str,
) -> None:
    for event in events:
        ticker = event.ticker.strip().upper()
        if not event.event_id.strip() or not ticker or not event.event_type.strip():
            raise ValueError("Market events require an ID, ticker, and event type")
        canonical = {
            "ticker": ticker,
            "event_type": event.event_type,
            "event_at": _iso(event.event_at),
            "published_at": _iso(event.published_at) if event.published_at else None,
            "effective_at": _iso(event.effective_at) if event.effective_at else None,
            "status": event.status,
            "source_url": event.source_url,
            "payload": event.payload,
        }
        version = event.version or _stable_id(canonical)[:24]
        database.execute(
            """
            INSERT INTO market_events(
                source,feed,event_id,version,ticker,event_type,event_at,published_at,
                effective_at,status,source_url,payload_json,first_run_id,last_run_id,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,feed,event_id,version) DO UPDATE SET
                ticker=excluded.ticker,event_type=excluded.event_type,
                event_at=excluded.event_at,published_at=excluded.published_at,
                effective_at=excluded.effective_at,status=excluded.status,
                source_url=excluded.source_url,payload_json=excluded.payload_json,
                last_run_id=excluded.last_run_id,last_collected_at=excluded.last_collected_at
            """,
            (
                fetch.source,
                fetch.feed,
                event.event_id.strip(),
                version,
                ticker,
                event.event_type.strip(),
                _iso(event.event_at),
                _iso(event.published_at) if event.published_at else None,
                _iso(event.effective_at) if event.effective_at else None,
                event.status,
                event.source_url,
                _json(event.payload),
                run_id,
                run_id,
                collected_at,
                collected_at,
            ),
        )
        _record_batch_item(
            database,
            run_id,
            f"event:{event.event_id}:{version}",
            {"ticker": ticker, "event_type": event.event_type, "version": version},
        )


def _security_quote_projection(
    database: Any,
    run_id: str,
    fetch: SourceFetch,
    quotes: tuple[SecurityQuote, ...],
    collected_at: str,
) -> None:
    for quote in quotes:
        ticker = quote.ticker.strip().upper()
        if not ticker:
            raise ValueError("Security quotes require a ticker")
        values = [
            _number(value)
            for value in (
                quote.bid,
                quote.ask,
                quote.bid_size,
                quote.ask_size,
                quote.last_trade,
            )
        ]
        observed_at = _iso(quote.observed_at)
        database.execute(
            """
            INSERT INTO security_quotes(
                source,feed,ticker,observed_at,bid,ask,bid_size,ask_size,last_trade,
                exchange,conditions_json,payload_json,first_run_id,last_run_id,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,feed,ticker,observed_at) DO UPDATE SET
                bid=excluded.bid,ask=excluded.ask,bid_size=excluded.bid_size,
                ask_size=excluded.ask_size,last_trade=excluded.last_trade,
                exchange=excluded.exchange,conditions_json=excluded.conditions_json,
                payload_json=excluded.payload_json,last_run_id=excluded.last_run_id,
                last_collected_at=excluded.last_collected_at
            """,
            (
                fetch.source,
                fetch.feed,
                ticker,
                observed_at,
                *values,
                quote.exchange,
                _json(quote.conditions),
                _json(quote.payload),
                run_id,
                run_id,
                collected_at,
                collected_at,
            ),
        )
        _record_batch_item(
            database,
            run_id,
            f"quote:{ticker}:{observed_at}",
            {"ticker": ticker, "observed_at": observed_at},
        )


def _issuer_fact_projection(
    database: Any,
    run_id: str,
    fetch: SourceFetch,
    facts: tuple[IssuerFact, ...],
    collected_at: str,
) -> None:
    for fact in facts:
        value = _number(fact.value)
        if fact.cik < 1 or not fact.concept.strip() or value is None:
            raise ValueError("Issuer facts require a CIK, concept, and finite value")
        identity = {
            "source": fetch.source,
            "cik": fact.cik,
            "concept": fact.concept,
            "value": value,
            "unit": fact.unit,
            "period_start": fact.period_start.isoformat() if fact.period_start else None,
            "period_end": fact.period_end.isoformat(),
            "filed_at": _iso(fact.filed_at),
            "accession": fact.accession,
        }
        fact_id = _stable_id(identity)
        database.execute(
            """
            INSERT INTO issuer_facts(
                id,source,feed,cik,concept,value,unit,period_start,period_end,filed_at,
                accession,form,source_tag,payload_json,first_run_id,last_run_id,
                first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                form=excluded.form,source_tag=excluded.source_tag,
                payload_json=excluded.payload_json,last_run_id=excluded.last_run_id,
                last_collected_at=excluded.last_collected_at
            """,
            (
                fact_id,
                fetch.source,
                fetch.feed,
                fact.cik,
                fact.concept.strip(),
                value,
                fact.unit,
                fact.period_start.isoformat() if fact.period_start else None,
                fact.period_end.isoformat(),
                _iso(fact.filed_at),
                fact.accession,
                fact.form,
                fact.source_tag,
                _json(fact.payload),
                run_id,
                run_id,
                collected_at,
                collected_at,
            ),
        )
        _record_batch_item(
            database,
            run_id,
            f"fact:{fact_id}",
            {"cik": fact.cik, "concept": fact.concept, "filed_at": _iso(fact.filed_at)},
        )


def _entity_link_projection(
    database: Any,
    run_id: str,
    fetch: SourceFetch,
    links: tuple[EntityLink, ...],
    collected_at: str,
) -> None:
    for link in links:
        ticker = link.ticker.strip().upper()
        if not link.external_id.strip() or not ticker or not 0 <= link.confidence <= 1:
            raise ValueError("Entity links require an ID, ticker, and confidence from 0 to 1")
        identity = {
            "source": fetch.source,
            "external_id": link.external_id,
            "ticker": ticker,
            "valid_from": link.valid_from.isoformat() if link.valid_from else None,
        }
        link_id = _stable_id(identity)
        database.execute(
            """
            INSERT INTO entity_links(
                id,source,feed,external_id,cik,ticker,confidence,method,valid_from,
                valid_to,payload_json,first_run_id,last_run_id,first_collected_at,
                last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                cik=excluded.cik,confidence=excluded.confidence,method=excluded.method,
                valid_to=excluded.valid_to,payload_json=excluded.payload_json,
                last_run_id=excluded.last_run_id,last_collected_at=excluded.last_collected_at
            """,
            (
                link_id,
                fetch.source,
                fetch.feed,
                link.external_id.strip(),
                link.cik,
                ticker,
                link.confidence,
                link.method,
                link.valid_from.isoformat() if link.valid_from else None,
                link.valid_to.isoformat() if link.valid_to else None,
                _json(link.payload),
                run_id,
                run_id,
                collected_at,
                collected_at,
            ),
        )
        _record_batch_item(
            database,
            run_id,
            f"link:{link_id}",
            {"external_id": link.external_id, "ticker": ticker},
        )


def _macro_observation_projection(
    database: Any,
    run_id: str,
    fetch: SourceFetch,
    observations: tuple[MacroObservation, ...],
    collected_at: str,
) -> None:
    for observation in observations:
        value = _number(observation.value)
        if not observation.series_id.strip() or value is None:
            raise ValueError("Macro observations require a series ID and finite value")
        observation_date = observation.observation_date.isoformat()
        vintage_date = observation.vintage_date.isoformat()
        database.execute(
            """
            INSERT INTO macro_observations(
                source,feed,series_id,observation_date,vintage_date,value,payload_json,
                first_run_id,last_run_id,first_collected_at,last_collected_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source,series_id,observation_date,vintage_date) DO UPDATE SET
                feed=excluded.feed,value=excluded.value,payload_json=excluded.payload_json,
                last_run_id=excluded.last_run_id,last_collected_at=excluded.last_collected_at
            """,
            (
                fetch.source,
                fetch.feed,
                observation.series_id.strip(),
                observation_date,
                vintage_date,
                value,
                _json(observation.payload),
                run_id,
                run_id,
                collected_at,
                collected_at,
            ),
        )
        _record_batch_item(
            database,
            run_id,
            f"macro:{observation.series_id}:{observation_date}:{vintage_date}",
            {"series_id": observation.series_id, "observation_date": observation_date},
        )


def _normalized_projection(
    database: Any,
    run_id: str,
    batch: SourceBatch,
    collected_at: str,
) -> None:
    fetch = batch.fetch
    _market_event_projection(database, run_id, fetch, batch.market_events, collected_at)
    _security_quote_projection(database, run_id, fetch, batch.security_quotes, collected_at)
    _issuer_fact_projection(database, run_id, fetch, batch.issuer_facts, collected_at)
    _entity_link_projection(database, run_id, fetch, batch.entity_links, collected_at)
    _macro_observation_projection(
        database,
        run_id,
        fetch,
        batch.macro_observations,
        collected_at,
    )


def record_source_batch(batch: SourceBatch) -> str:
    """Atomically record a raw fetch and every normalized row derived from it."""

    fetch = batch.fetch
    run_id = uuid.uuid4().hex
    metadata_json = _json(fetch.metadata)
    requested_count = _requested_count(fetch)
    received_count = _batch_size(batch) or _received_count(fetch)
    projection_error: Exception | None = None
    with connection() as database:
        _ensure_source_policy(database, fetch, _iso(fetch.finished_at))
        database.execute(
            """
            INSERT INTO ingestion_runs(
                id,source,feed,locator,status,requested_count,received_count,
                content_hash,content_type,metadata_json,error,started_at,finished_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                fetch.source,
                fetch.feed,
                fetch.locator,
                "recording" if fetch.status != "error" else "error",
                requested_count,
                received_count,
                None,
                fetch.content_type,
                metadata_json,
                fetch.error,
                _iso(fetch.started_at),
                _iso(fetch.finished_at),
            ),
        )
        if fetch.status != "error":
            database.execute("SAVEPOINT source_projection")
            collected_at = _iso(fetch.finished_at)
            try:
                if isinstance(fetch.payload, bytes):
                    content_hash = hashlib.sha256(fetch.payload).hexdigest()
                    _archive_document(database, fetch, content_hash, collected_at)
                elif fetch.source == "yahoo" and fetch.feed == "market_bars":
                    content_hash = _market_projection(database, run_id, fetch, collected_at)
                elif fetch.source == "yahoo" and fetch.feed == "universe":
                    content_hash = _universe_projection(database, run_id, fetch)
                else:
                    content_hash = hashlib.sha256(_json(fetch.payload).encode()).hexdigest()
                _normalized_projection(database, run_id, batch, collected_at)
                database.execute("RELEASE SAVEPOINT source_projection")
                database.execute(
                    """
                    UPDATE ingestion_runs SET status=?,content_hash=?,error=NULL WHERE id=?
                    """,
                    (fetch.status, content_hash, run_id),
                )
            except Exception as exc:
                database.execute("ROLLBACK TO SAVEPOINT source_projection")
                database.execute("RELEASE SAVEPOINT source_projection")
                database.execute(
                    """
                    UPDATE ingestion_runs SET status='error',error=? WHERE id=?
                    """,
                    (f"Projection failed: {exc}"[:1000], run_id),
                )
                projection_error = exc
    if projection_error is not None:
        raise projection_error
    return run_id


def record_source_fetch(fetch: SourceFetch) -> str:
    """Backward-compatible wrapper for a fetch with no extra normalized rows."""

    return record_source_batch(SourceBatch(fetch=fetch))


def mark_source_item(
    *,
    source: str,
    feed: str,
    item_key: str,
    status: str,
    payload: dict[str, Any] | None = None,
    error: str | None = None,
    parser_version: str | None = None,
) -> None:
    timestamp = _iso()
    processed_at = timestamp if status in TERMINAL_ITEM_STATUSES else None
    with connection() as database:
        database.execute(
            """
            INSERT INTO source_item_state(
                source,feed,item_key,status,payload_json,error,parser_version,
                attempt_count,first_seen_at,last_seen_at,processed_at
            ) VALUES(?,?,?,?,?,?,?,1,?,?,?)
            ON CONFLICT(source,feed,item_key) DO UPDATE SET
                status=excluded.status,payload_json=excluded.payload_json,
                error=excluded.error,parser_version=excluded.parser_version,
                attempt_count=source_item_state.attempt_count+1,
                last_seen_at=excluded.last_seen_at,processed_at=excluded.processed_at
            """,
            (
                source,
                feed,
                item_key,
                status,
                _json(payload or {}),
                error[:1000] if error else None,
                parser_version,
                timestamp,
                timestamp,
                processed_at,
            ),
        )


def source_item_is_terminal(source: str, feed: str, item_key: str) -> bool:
    with connection() as database:
        row = database.execute(
            """
            SELECT status FROM source_item_state
            WHERE source=? AND feed=? AND item_key=?
            """,
            (source, feed, item_key),
        ).fetchone()
    return bool(row and row["status"] in TERMINAL_ITEM_STATUSES)


def _datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _schedule_active(schedule: str, at: datetime) -> bool:
    if schedule == "always":
        return True
    if schedule == "us_extended_weekdays":
        eastern = at.astimezone(ZoneInfo("America/New_York"))
        minutes = eastern.hour * 60 + eastern.minute
        return eastern.weekday() < 5 and 4 * 60 <= minutes < 20 * 60
    if schedule == "weekday_daily":
        return at.astimezone(ZoneInfo("America/New_York")).weekday() < 5
    return False


def _source_health(
    policy: dict[str, Any],
    run: dict[str, Any] | None,
    as_of: datetime,
) -> dict[str, Any]:
    last_success = _datetime(run.get("last_success_at") if run else None)
    last_error = _datetime(run.get("last_error_at") if run else None)
    active_now = _schedule_active(str(policy["schedule"]), as_of)
    stale_seconds = policy.get("stale_after_seconds")
    stale_at = (
        last_success + timedelta(seconds=int(stale_seconds))
        if last_success and stale_seconds is not None
        else None
    )
    if not policy["enabled"]:
        health = "disabled"
    elif last_success is None:
        health = "error" if last_error else "pending"
    elif last_error and last_error > last_success:
        health = "error"
    elif stale_seconds is None:
        health = "healthy"
    elif not active_now:
        health = "idle"
    elif stale_at and as_of > stale_at:
        health = "stale"
    else:
        health = "healthy"
    return {
        "health": health,
        "active_now": active_now,
        "stale_at": stale_at.isoformat() if stale_at else None,
        "age_seconds": (
            max(0, round((as_of - last_success).total_seconds())) if last_success else None
        ),
    }


def ingestion_status(as_of: datetime | None = None) -> dict[str, Any]:
    """Return a small operational view without exposing stored source payloads."""

    checked_at = as_of or datetime.now(UTC)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=UTC)
    with connection() as database:
        run_rows = database.execute(
            """
            SELECT source,feed,COUNT(*) AS runs,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS successful,
                   SUM(CASE WHEN status='partial' THEN 1 ELSE 0 END) AS partial,
                   SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed,
                   MAX(CASE WHEN status IN ('success','partial') THEN finished_at END)
                       AS last_success_at,
                   MAX(CASE WHEN status='error' THEN finished_at END) AS last_error_at
            FROM ingestion_runs GROUP BY source,feed ORDER BY source,feed
            """
        ).fetchall()
        item_rows = database.execute(
            """
            SELECT source,feed,status,COUNT(*) AS items
            FROM source_item_state GROUP BY source,feed,status
            ORDER BY source,feed,status
            """
        ).fetchall()
        registry_rows = database.execute(
            """
            SELECT source,feed,title,owner,terms_url,credential_env,
                   expected_cadence_seconds,stale_after_seconds,schedule,
                   storage_policy,display_policy,attribution,review_status,enabled,
                   created_at,updated_at
            FROM source_registry ORDER BY source,feed
            """
        ).fetchall()
    runs = {(row["source"], row["feed"]): dict(row) for row in run_rows}
    sources: list[dict[str, Any]] = []
    for row in registry_rows:
        source = dict(row)
        source["enabled"] = bool(source["enabled"])
        run = runs.get((source["source"], source["feed"]))
        source.update(
            run
            or {
                "runs": 0,
                "successful": 0,
                "partial": 0,
                "failed": 0,
                "last_success_at": None,
                "last_error_at": None,
            }
        )
        source.update(_source_health(source, run, checked_at))
        sources.append(source)
    source_by_key = {(row["source"], row["feed"]): row for row in sources}
    feeds: list[dict[str, Any]] = []
    for row in run_rows:
        feed = dict(row)
        policy = source_by_key.get((feed["source"], feed["feed"]))
        if policy:
            feed.update(
                {
                    "health": policy["health"],
                    "active_now": policy["active_now"],
                    "stale_at": policy["stale_at"],
                    "age_seconds": policy["age_seconds"],
                    "review_status": policy["review_status"],
                }
            )
        feeds.append(feed)
    health_counts: dict[str, int] = {}
    for source in sources:
        health = str(source["health"])
        health_counts[health] = health_counts.get(health, 0) + 1
    return {
        "checked_at": checked_at.isoformat(),
        "summary": health_counts,
        "sources": sources,
        "feeds": feeds,
        "item_states": [dict(row) for row in item_rows],
    }
