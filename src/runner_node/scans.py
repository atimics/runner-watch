from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runner_watch.market_data import routed_market_data
from runner_watch.models import ScanSettings
from runner_watch.scanner import RunnerScanner
from runner_watch.universe import (
    broad_us_universe,
    parse_custom_symbols,
    penny_runner_universe,
    starter_universe,
)


class ScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    universe: Literal["penny", "starter", "broad", "custom"] = "penny"
    symbols: list[str] = Field(default_factory=list, max_length=500)
    min_price: float = Field(default=0.2, ge=0)
    max_price: float = Field(default=5, gt=0)
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
    def __init__(self, maximum: int = 20, database_path: str | Path | None = None) -> None:
        self.maximum = maximum
        self._lock = Lock()
        self._scans: dict[str, dict[str, object]] = {}
        self.database_path = Path(database_path).expanduser() if database_path else None
        if self.database_path is not None:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.database_path) as database:
                database.execute(
                    """
                    CREATE TABLE IF NOT EXISTS node_scan_receipts (
                        id TEXT PRIMARY KEY,
                        finished_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    )
                    """
                )
                stale_ids: list[str] = []
                for scan_id, payload_json in database.execute(
                    "SELECT id,payload_json FROM node_scan_receipts"
                ):
                    try:
                        source = json.loads(payload_json).get("source")
                    except (AttributeError, TypeError, ValueError):
                        source = None
                    if source != "live":
                        stale_ids.append(scan_id)
                database.executemany(
                    "DELETE FROM node_scan_receipts WHERE id=?",
                    ((scan_id,) for scan_id in stale_ids),
                )

    def save(self, payload: dict[str, object]) -> dict[str, object]:
        if payload.get("source") != "live":
            raise ValueError("Only live scan receipts can be stored")
        scan_id = f"scan_{secrets.token_urlsafe(12)}"
        value = {"id": scan_id, **payload}
        with self._lock:
            if self.database_path is None:
                self._scans[scan_id] = value
                while len(self._scans) > self.maximum:
                    self._scans.pop(next(iter(self._scans)))
            else:
                with sqlite3.connect(self.database_path) as database:
                    database.execute(
                        "INSERT INTO node_scan_receipts(id,finished_at,payload_json) VALUES(?,?,?)",
                        (
                            scan_id,
                            str(value.get("finished_at") or datetime.now(UTC).isoformat()),
                            json.dumps(value, separators=(",", ":")),
                        ),
                    )
                    database.execute(
                        """
                        DELETE FROM node_scan_receipts
                        WHERE id NOT IN (
                            SELECT id FROM node_scan_receipts
                            ORDER BY finished_at DESC, id DESC LIMIT ?
                        )
                        """,
                        (self.maximum,),
                    )
        return value

    def get(self, scan_id: str) -> dict[str, object] | None:
        with self._lock:
            if self.database_path is None:
                return self._scans.get(scan_id)
            with sqlite3.connect(self.database_path) as database:
                row = database.execute(
                    "SELECT payload_json FROM node_scan_receipts WHERE id=?", (scan_id,)
                ).fetchone()
            return json.loads(row[0]) if row else None

    def list(self) -> list[dict[str, object]]:
        with self._lock:
            if self.database_path is None:
                return list(reversed(list(self._scans.values())))
            with sqlite3.connect(self.database_path) as database:
                rows = database.execute(
                    """
                    SELECT payload_json FROM node_scan_receipts
                    ORDER BY finished_at DESC, id DESC LIMIT ?
                    """,
                    (self.maximum,),
                ).fetchall()
            return [json.loads(row[0]) for row in rows]


def run_scan(
    request: ScanRequest,
    provider_keys: Mapping[str, str] | None = None,
    provider_routes: Mapping[str, list[str]] | None = None,
) -> dict[str, object]:
    warnings: list[str] = []
    provider = routed_market_data(
        provider_keys=provider_keys,
        provider_order=(provider_routes or {}).get("market_bars"),
    )
    if request.universe == "penny":
        entries, warnings = penny_runner_universe(
            min_price=request.min_price,
            max_price=request.max_price,
        )
        symbols = [item.symbol for item in entries]
    elif request.universe == "starter":
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
        "source": "live",
        "started_at": started_at.isoformat(),
        "finished_at": result.finished_at.isoformat(),
        "elapsed_seconds": result.elapsed_seconds,
        "requested_symbols": result.requested_symbols,
        "liquid_symbols": result.liquid_symbols,
        "scanned_symbols": result.scanned_symbols,
        "rows": [row.to_dict() for row in result.rows],
        "warnings": list(dict.fromkeys([*warnings, *result.warnings])),
    }
