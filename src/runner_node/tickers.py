from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

from runner_watch.market_data import DownloadResult, routed_market_data
from runner_watch.provider_contracts import ProviderProvenance
from runner_watch.scanner import analyze_ticker, build_daily_profile
from runner_watch.universe import normalize_symbol

TICKER_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9-]{0,14}")
NASDAQ_API = "https://api.nasdaq.com/api/quote"


def validate_ticker(value: str) -> str:
    symbol = normalize_symbol(value)
    if not TICKER_PATTERN.fullmatch(symbol):
        raise ValueError("Ticker must contain only letters, numbers, or a dash")
    return symbol


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _market_number(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.replace("$", "").replace("%", "").replace(",", "").strip()
    return _number(value)


def _nasdaq_json(path: str, params: Mapping[str, str], *, timeout: float = 8.0) -> dict[str, Any]:
    url = f"{NASDAQ_API}/{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; RATi-Swarm/0.1; +https://github.com/atimics/runner-watch)",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    status = payload.get("status") or {}
    data = payload.get("data")
    if status.get("rCode") != 200 or not isinstance(data, dict):
        messages = status.get("bCodeMessage") or []
        detail = "; ".join(
            str(item.get("errorMessage"))
            for item in messages
            if isinstance(item, dict) and item.get("errorMessage")
        )
        raise LookupError(detail or "Nasdaq returned no usable ticker data")
    return data


def _nasdaq_ticker_results(symbol: str) -> tuple[DownloadResult, DownloadResult]:
    now = datetime.now(UTC)
    start = (now - timedelta(days=370)).date().isoformat()
    end = now.date().isoformat()
    daily_error: Exception | None = None
    chart_error: Exception | None = None
    try:
        chart_data = _nasdaq_json(
            f"{urllib.parse.quote(symbol)}/chart",
            {"assetclass": "stocks"},
        )
    except Exception as error:
        chart_data = {}
        chart_error = error
    try:
        daily_data = _nasdaq_json(
            f"{urllib.parse.quote(symbol)}/historical",
            {
                "assetclass": "stocks",
                "fromdate": start,
                "todate": end,
                "limit": "260",
            },
        )
    except Exception as error:
        daily_data = {}
        daily_error = error

    daily_rows = (daily_data.get("tradesTable") or {}).get("rows") or []
    daily_values = [
        {
            "timestamp": pd.to_datetime(row.get("date"), format="%m/%d/%Y", utc=True),
            "Open": _market_number(row.get("open")),
            "High": _market_number(row.get("high")),
            "Low": _market_number(row.get("low")),
            "Close": _market_number(row.get("close")),
            "Volume": _market_number(row.get("volume")),
        }
        for row in daily_rows
        if isinstance(row, dict) and row.get("date") and _market_number(row.get("close"))
    ]
    daily_frame = (
        pd.DataFrame(daily_values).set_index("timestamp").sort_index()
        if daily_values
        else pd.DataFrame()
    )

    minute_values = [
        {
            "timestamp": pd.to_datetime(point.get("x"), unit="ms", utc=True),
            "Price": _market_number(point.get("y")),
        }
        for point in chart_data.get("chart") or []
        if isinstance(point, dict) and point.get("x") and _market_number(point.get("y"))
    ]
    if minute_values:
        minute_frame = pd.DataFrame(minute_values).set_index("timestamp").sort_index()
        intraday_frame = (
            minute_frame["Price"]
            .resample("5min")
            .agg(Open="first", High="max", Low="min", Close="last")
            .dropna(how="all")
        )
        intraday_frame["Volume"] = math.nan
    else:
        intraday_frame = pd.DataFrame()

    collected_at = datetime.now(UTC)

    def result(
        frame: pd.DataFrame,
        locator: str,
        interval: str,
        error: Exception | None,
    ) -> DownloadResult:
        observed_at = (
            pd.Timestamp(frame.index[-1]).to_pydatetime().astimezone(UTC)
            if not frame.empty
            else None
        )
        warnings = []
        if error is not None:
            source_name = "intraday" if interval == "5m" else "daily"
            warnings.append(f"Nasdaq {source_name} ticker source failed: {error}")
        if interval == "5m" and not frame.empty:
            warnings.append(
                "Nasdaq's ticker chart does not publish volume per point; "
                "volume-based intraday analysis is unavailable."
            )
        return DownloadResult(
            frames={symbol: frame} if not frame.empty else {},
            failed=[] if not frame.empty else [symbol],
            warnings=warnings,
            provenance=ProviderProvenance(
                provider="nasdaq",
                feed="market_bars",
                locator=locator,
                observed_at=observed_at,
                as_of=observed_at or collected_at,
                collected_at=collected_at,
                delayed=True,
                quality={"interval": interval, "bars": len(frame)},
            ),
        )

    return (
        result(daily_frame, f"{NASDAQ_API}/{symbol}/historical", "1d", daily_error),
        result(intraday_frame, f"{NASDAQ_API}/{symbol}/chart", "5m", chart_error),
    )


def _column_name(frame: pd.DataFrame, wanted: str) -> Any | None:
    normalized = wanted.lower().replace(" ", "")
    return next(
        (column for column in frame.columns if str(column).lower().replace(" ", "") == normalized),
        None,
    )


def _timestamp(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(UTC)
    return timestamp.tz_convert(UTC).isoformat()


def frame_bars(frame: pd.DataFrame, *, limit: int) -> list[dict[str, object]]:
    columns = {
        name: _column_name(frame, name) for name in ("Open", "High", "Low", "Close", "Volume")
    }
    rows: list[dict[str, object]] = []
    for index, row in frame.sort_index().tail(limit).iterrows():
        close = _number(row.get(columns["Close"]))
        if close is None:
            continue
        rows.append(
            {
                "timestamp": _timestamp(index),
                "open": _number(row.get(columns["Open"])),
                "high": _number(row.get(columns["High"])),
                "low": _number(row.get(columns["Low"])),
                "close": close,
                "volume": _number(row.get(columns["Volume"])),
            }
        )
    return rows


def _pull(label: str, result: DownloadResult, bars: int) -> dict[str, object]:
    provenance = result.provenance
    return {
        "label": label,
        "provider": provenance.provider if provenance else "unavailable",
        "feed": provenance.feed if provenance else "market_bars",
        "status": "connected" if bars else "failed",
        "bars": bars,
        "delayed": provenance.delayed if provenance else None,
        "observed_at": provenance.observed_at.isoformat()
        if provenance and provenance.observed_at
        else None,
        "collected_at": provenance.collected_at.isoformat() if provenance else None,
        "fallback_used": provenance.fallback_used if provenance else False,
        "attempted_providers": list(provenance.attempted_providers) if provenance else [],
    }


def _use_fallback(
    fetch: Callable[[list[str]], DownloadResult],
    existing: DownloadResult,
    symbol: str,
    interval: str,
) -> DownloadResult:
    try:
        fallback = fetch([symbol])
    except Exception as error:
        existing.warnings.append(f"Market-data {interval} fallback failed: {error}")
        return existing
    fallback.warnings[:0] = existing.warnings
    if fallback.provenance is not None:
        attempted = tuple(
            dict.fromkeys(
                (
                    "nasdaq",
                    *fallback.provenance.attempted_providers,
                    fallback.provenance.provider,
                )
            )
        )
        fallback.provenance = fallback.provenance.model_copy(
            update={"attempted_providers": attempted, "fallback_used": True}
        )
    return fallback


def load_ticker_detail(
    ticker: str,
    provider_keys: Mapping[str, str] | None = None,
    provider_routes: Mapping[str, list[str]] | None = None,
) -> dict[str, object]:
    symbol = validate_ticker(ticker)
    try:
        daily_result, intraday_result = _nasdaq_ticker_results(symbol)
    except Exception as error:
        daily_result = DownloadResult(
            frames={},
            failed=[symbol],
            warnings=[f"Nasdaq daily ticker source failed: {error}"],
        )
        intraday_result = DownloadResult(
            frames={},
            failed=[symbol],
            warnings=[f"Nasdaq intraday ticker source failed: {error}"],
        )
    nasdaq_daily_frame = daily_result.frames.get(symbol)
    nasdaq_intraday_frame = intraday_result.frames.get(symbol)
    needs_daily_fallback = nasdaq_daily_frame is None or nasdaq_daily_frame.empty
    needs_intraday_fallback = nasdaq_intraday_frame is None or nasdaq_intraday_frame.empty
    if needs_daily_fallback or needs_intraday_fallback:
        provider = routed_market_data(
            provider_keys=provider_keys,
            provider_order=(provider_routes or {}).get("market_bars"),
        )
        try:
            if needs_daily_fallback:
                daily_result = _use_fallback(provider.daily, daily_result, symbol, "daily")
            if needs_intraday_fallback:
                intraday_result = _use_fallback(
                    provider.intraday,
                    intraday_result,
                    symbol,
                    "intraday",
                )
        finally:
            provider.close()

    daily_frame = daily_result.frames.get(symbol, pd.DataFrame())
    intraday_frame = intraday_result.frames.get(symbol, pd.DataFrame())
    daily_bars = frame_bars(daily_frame, limit=180)
    intraday_bars = frame_bars(intraday_frame, limit=500)
    if not daily_bars and not intraday_bars:
        raise LookupError(f"No live market data was returned for {symbol}")

    profile = build_daily_profile(symbol, daily_frame) if not daily_frame.empty else None
    volume_column = _column_name(intraday_frame, "Volume")
    has_intraday_volume = bool(
        volume_column is not None
        and not intraday_frame.empty
        and pd.to_numeric(intraday_frame[volume_column], errors="coerce").fillna(0).sum() > 0
    )
    snapshot = (
        analyze_ticker(symbol, profile, intraday_frame)
        if profile is not None and not intraday_frame.empty and has_intraday_volume
        else None
    )
    latest = intraday_bars[-1] if intraday_bars else daily_bars[-1]
    price = float(latest["close"])
    previous_close = profile.previous_close if profile else None
    change_pct = (
        (price / previous_close - 1) * 100 if previous_close and previous_close > 0 else None
    )
    warnings = list(dict.fromkeys([*daily_result.warnings, *intraday_result.warnings]))
    return {
        "ticker": symbol,
        "source": "local_scanner",
        "fetched_at": datetime.now(UTC).isoformat(),
        "quote": {
            "price": snapshot.price if snapshot else price,
            "change_pct": snapshot.change_pct if snapshot else change_pct,
            "quote_time": snapshot.quote_time.isoformat() if snapshot else str(latest["timestamp"]),
            "session": snapshot.session if snapshot else "MARKET DATA",
        },
        "analysis": snapshot.to_dict() if snapshot else None,
        "charts": {"intraday": intraday_bars, "daily": daily_bars},
        "pulls": [
            _pull("Daily history", daily_result, len(daily_bars)),
            _pull("Five-minute history", intraday_result, len(intraday_bars)),
        ],
        "warnings": warnings,
    }
