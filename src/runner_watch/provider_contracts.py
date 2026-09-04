from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DataKind(StrEnum):
    BARS = "bars"
    QUOTES = "quotes"
    MARKET_EVENTS = "market_events"
    ISSUER_FACTS = "issuer_facts"
    MACRO_OBSERVATIONS = "macro_observations"


class CanonicalModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Provider timestamps must include a timezone")
    return value.astimezone(UTC)


class Bar(CanonicalModel):
    symbol: str
    interval: str
    timestamp: datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol:
            raise ValueError("A bar must have a symbol")
        return symbol

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return _utc(value)


class Quote(CanonicalModel):
    symbol: str
    observed_at: datetime
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    last: float | None = None
    exchange: str | None = None
    conditions: tuple[str, ...] = ()

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not symbol:
            raise ValueError("A quote must have a symbol")
        return symbol

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return _utc(value)


class MarketEvent(CanonicalModel):
    event_id: str
    symbol: str
    event_type: str
    event_at: datetime
    status: str
    source_url: str
    published_at: datetime | None = None
    effective_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("event_at", "published_at", "effective_at")
    @classmethod
    def normalize_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


class IssuerFact(CanonicalModel):
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
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("filed_at")
    @classmethod
    def normalize_filed_at(cls, value: datetime) -> datetime:
        return _utc(value)


class MacroObservation(CanonicalModel):
    series_id: str
    observation_date: date
    vintage_date: date
    value: float
    payload: dict[str, Any] = Field(default_factory=dict)


class ProviderRequest(CanonicalModel):
    kind: DataKind
    symbols: tuple[str, ...] = ()
    interval: str | None = None
    period: str | None = None
    extended_hours: bool = False
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        return tuple(
            dict.fromkeys(str(item).strip().upper() for item in value if str(item).strip())
        )

    @model_validator(mode="after")
    def validate_request(self) -> ProviderRequest:
        if self.kind in {DataKind.BARS, DataKind.QUOTES} and not self.symbols:
            raise ValueError(f"{self.kind.value} requests require at least one symbol")
        if self.kind == DataKind.BARS and not self.interval:
            raise ValueError("Bar requests require an interval")
        return self


class ProviderProvenance(CanonicalModel):
    provider: str
    feed: str
    locator: str
    observed_at: datetime | None = None
    as_of: datetime
    collected_at: datetime
    delayed: bool | None = None
    raw_document_reference: str | None = None
    attempted_providers: tuple[str, ...] = ()
    fallback_used: bool = False
    warnings: tuple[str, ...] = ()
    quality: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", "feed", "locator")
    @classmethod
    def require_identity(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Provider provenance identity fields cannot be empty")
        return clean

    @field_validator("observed_at", "as_of", "collected_at")
    @classmethod
    def normalize_times(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None


class FetchBatch(CanonicalModel):
    request: ProviderRequest
    status: Literal["success", "partial", "error"]
    provenance: ProviderProvenance
    bars: tuple[Bar, ...] = ()
    quotes: tuple[Quote, ...] = ()
    market_events: tuple[MarketEvent, ...] = ()
    issuer_facts: tuple[IssuerFact, ...] = ()
    macro_observations: tuple[MacroObservation, ...] = ()
    error: str | None = None

    @property
    def item_count(self) -> int:
        return sum(
            len(items)
            for items in (
                self.bars,
                self.quotes,
                self.market_events,
                self.issuer_facts,
                self.macro_observations,
            )
        )

    @model_validator(mode="after")
    def validate_status(self) -> FetchBatch:
        if self.status == "error" and not self.error:
            raise ValueError("An error batch must explain the error")
        records_by_kind = {
            DataKind.BARS: self.bars,
            DataKind.QUOTES: self.quotes,
            DataKind.MARKET_EVENTS: self.market_events,
            DataKind.ISSUER_FACTS: self.issuer_facts,
            DataKind.MACRO_OBSERVATIONS: self.macro_observations,
        }
        unexpected = [
            kind.value
            for kind, records in records_by_kind.items()
            if kind != self.request.kind and records
        ]
        if unexpected:
            raise ValueError(
                f"A {self.request.kind.value} request returned unexpected records: "
                f"{', '.join(unexpected)}"
            )
        return self


class ProviderAdapter(Protocol):
    name: str
    capabilities: frozenset[DataKind]

    def fetch(self, request: ProviderRequest, progress: Any = None) -> FetchBatch: ...
