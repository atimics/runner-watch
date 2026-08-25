from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

FetchStatus = Literal["success", "partial", "error"]


@dataclass(slots=True)
class SourceFetch:
    """One completed attempt to read data from an outside source."""

    source: str
    feed: str
    locator: str
    status: FetchStatus
    started_at: datetime
    finished_at: datetime
    payload: Any = None
    content_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def success(
        cls,
        *,
        source: str,
        feed: str,
        locator: str,
        started_at: datetime,
        payload: Any,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        partial: bool = False,
    ) -> SourceFetch:
        return cls(
            source=source,
            feed=feed,
            locator=locator,
            status="partial" if partial else "success",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            payload=payload,
            content_type=content_type,
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        *,
        source: str,
        feed: str,
        locator: str,
        started_at: datetime,
        error: Exception | str,
        metadata: dict[str, Any] | None = None,
    ) -> SourceFetch:
        return cls(
            source=source,
            feed=feed,
            locator=locator,
            status="error",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            metadata=metadata or {},
            error=str(error)[:1000],
        )


class SourceFetchRecorder(Protocol):
    def __call__(self, fetch: SourceFetch) -> None: ...


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """One source event, kept with its source ID and point-in-time timestamps."""

    event_id: str
    ticker: str
    event_type: str
    event_at: datetime
    status: str
    source_url: str
    published_at: datetime | None = None
    effective_at: datetime | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    version: str | None = None


@dataclass(frozen=True, slots=True)
class SecurityQuote:
    """A source-labelled quote snapshot. A quote is not assumed to be an NBBO."""

    ticker: str
    observed_at: datetime
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last_trade: float | None = None
    exchange: str | None = None
    conditions: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IssuerFact:
    """A filed issuer fact with enough timing data for point-in-time training."""

    cik: int
    concept: str
    value: float
    unit: str
    period_end: date
    filed_at: datetime
    accession: str
    form: str | None = None
    period_start: date | None = None
    source_tag: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EntityLink:
    """A reviewed link between a source entity and a listed issuer."""

    external_id: str
    ticker: str
    confidence: float
    method: str
    cik: int | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MacroObservation:
    """One macro value with its publication vintage preserved."""

    series_id: str
    observation_date: date
    vintage_date: date
    value: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SourceBatch:
    """A raw fetch and every normalized record derived from that exact response."""

    fetch: SourceFetch
    market_events: tuple[MarketEvent, ...] = ()
    security_quotes: tuple[SecurityQuote, ...] = ()
    issuer_facts: tuple[IssuerFact, ...] = ()
    entity_links: tuple[EntityLink, ...] = ()
    macro_observations: tuple[MacroObservation, ...] = ()
