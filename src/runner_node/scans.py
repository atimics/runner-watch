from __future__ import annotations

import secrets
from datetime import UTC, datetime
from threading import Lock
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from runner_watch.market_data import routed_market_data
from runner_watch.models import ScanSettings
from runner_watch.sample_data import SAMPLE_SYMBOLS, SampleMarketData
from runner_watch.scanner import RunnerScanner
from runner_watch.universe import broad_us_universe, parse_custom_symbols, starter_universe


class ScanRequest(BaseModel):
    source: Literal["sample", "live"] = "sample"
    universe: Literal["starter", "broad", "custom"] = "starter"
    symbols: list[str] = Field(default_factory=list, max_length=500)
    min_price: float = Field(default=0.5, ge=0)
    max_price: float = Field(default=50, gt=0)
    min_avg_volume: int = Field(default=100_000, ge=0)
    min_avg_dollar_volume: float = Field(default=500_000, ge=0)
    max_symbols: int = Field(default=300, ge=1, le=5_000)
    top_n: int = Field(default=50, ge=1, le=100)
    crash_only: bool = False

    @model_validator(mode="after")
    def validate_range_and_symbols(self) -> ScanRequest:
        if self.max_price <= self.min_price:
            raise ValueError("max_price must be greater than min_price")
        if self.universe == "custom" and not self.symbols:
            raise ValueError("custom scans require at least one symbol")
        return self


class ScanStore:
    """Bounded in-memory result store for the first standalone scanner API."""

    def __init__(self, maximum: int = 20) -> None:
        self.maximum = maximum
        self._lock = Lock()
        self._scans: dict[str, dict[str, object]] = {}

    def save(self, payload: dict[str, object]) -> dict[str, object]:
        scan_id = f"scan_{secrets.token_urlsafe(12)}"
        value = {"id": scan_id, **payload}
        with self._lock:
            self._scans[scan_id] = value
            while len(self._scans) > self.maximum:
                self._scans.pop(next(iter(self._scans)))
        return value

    def get(self, scan_id: str) -> dict[str, object] | None:
        with self._lock:
            return self._scans.get(scan_id)


def run_scan(request: ScanRequest) -> dict[str, object]:
    warnings: list[str] = []
    if request.source == "sample":
        provider = SampleMarketData()
        symbols = SAMPLE_SYMBOLS
    else:
        provider = routed_market_data()
        if request.universe == "starter":
            symbols = [item.symbol for item in starter_universe()]
        elif request.universe == "broad":
            entries, warnings = broad_us_universe()
            symbols = [item.symbol for item in entries]
        else:
            symbols = parse_custom_symbols(" ".join(request.symbols))

    settings = ScanSettings(
        min_price=request.min_price,
        max_price=request.max_price,
        min_avg_volume=request.min_avg_volume,
        min_avg_dollar_volume=request.min_avg_dollar_volume,
        max_symbols=request.max_symbols,
        top_n=request.top_n,
        crash_only=request.crash_only,
    )
    started_at = datetime.now(UTC)
    try:
        result = RunnerScanner(provider).scan(symbols, settings)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    return {
        "status": "complete",
        "source": request.source,
        "started_at": started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "elapsed_seconds": result.elapsed_seconds,
        "requested_symbols": result.requested_symbols,
        "liquid_symbols": result.liquid_symbols,
        "scanned_symbols": result.scanned_symbols,
        "rows": [row.to_dict() for row in result.rows],
        "warnings": list(dict.fromkeys([*warnings, *result.warnings])),
    }
