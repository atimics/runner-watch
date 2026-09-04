from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from runner_watch.chart_features import analyze_market_structure
from runner_watch.market_data import DownloadResult
from runner_watch.models import DailyProfile, RunnerSnapshot, ScanResult, ScanSettings
from runner_watch.risk import RiskInput, assess_risk
from runner_watch.scoring import ScoreInput, score_runner
from runner_watch.universe import normalize_symbol

EASTERN = ZoneInfo("America/New_York")
ProgressCallback = Callable[[int, int, str], None]


class MarketDataProvider(Protocol):
    def daily(
        self, tickers: list[str], progress: ProgressCallback | None = None
    ) -> DownloadResult: ...

    def intraday(
        self, tickers: list[str], progress: ProgressCallback | None = None
    ) -> DownloadResult: ...


def _column(frame: pd.DataFrame, name: str) -> pd.Series:
    for column in frame.columns:
        if str(column).lower().replace(" ", "") == name.lower().replace(" ", ""):
            return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(dtype="float64")


def _to_eastern_index(frame: pd.DataFrame) -> pd.DataFrame:
    clean = frame.copy()
    index = pd.to_datetime(clean.index)
    if index.tz is None:
        index = index.tz_localize(EASTERN)
    else:
        index = index.tz_convert(EASTERN)
    clean.index = index
    return clean.sort_index()


def build_daily_profile(
    ticker: str, frame: pd.DataFrame, now: datetime | None = None
) -> DailyProfile | None:
    if frame.empty:
        return None
    now_et = (now or datetime.now(UTC)).astimezone(EASTERN)
    clean = frame.copy()
    clean.index = pd.to_datetime(clean.index)
    completed = clean[clean.index.date < now_et.date()]
    if completed.empty:
        completed = clean.iloc[:-1] if len(clean) > 1 else clean
    completed = completed.tail(260)
    close = _column(completed, "Close").dropna()
    high = _column(completed, "High").dropna()
    low = _column(completed, "Low").dropna()
    volume = _column(completed, "Volume").dropna()
    if close.empty or high.empty or low.empty or volume.empty:
        return None
    adjusted_close = _column(completed, "Adj Close").reindex(close.index)
    ratios = (adjusted_close / close).replace([float("inf"), float("-inf")], pd.NA)
    ratios = ratios.dropna()
    latest_ratio = float(ratios.iloc[-1]) if not ratios.empty and ratios.iloc[-1] > 0 else 1.0
    normalized_high = high.copy()
    normalized_low = low.copy()
    if latest_ratio > 0 and not ratios.empty:
        high_ratios = ratios.reindex(high.index).ffill().bfill().fillna(latest_ratio)
        low_ratios = ratios.reindex(low.index).ffill().bfill().fillna(latest_ratio)
        normalized_high = high * high_ratios / latest_ratio
        normalized_low = low * low_ratios / latest_ratio
    previous_close = float(close.iloc[-1])
    previous_high = float(high.iloc[-1])
    average_volume = float(volume.tail(20).median())
    if not all(math.isfinite(item) and item > 0 for item in (previous_close, previous_high)):
        return None
    if not math.isfinite(average_volume) or average_volume < 0:
        return None
    return DailyProfile(
        ticker=ticker,
        previous_close=previous_close,
        previous_high=previous_high,
        average_volume=average_volume,
        average_dollar_volume=average_volume * previous_close,
        high_20d=float(normalized_high.tail(20).max()),
        high_90d=float(normalized_high.tail(90).max()),
        high_52w=float(normalized_high.tail(252).max()),
        low_20d=float(normalized_low.tail(20).min()),
    )


def _session_name(now_et: datetime) -> str:
    if now_et.weekday() >= 5:
        return "CLOSED"
    clock = now_et.time().replace(tzinfo=None)
    if time(4) <= clock < time(9, 30):
        return "PRE-MARKET"
    if time(9, 30) <= clock < time(16):
        return "REGULAR"
    if time(16) <= clock < time(20):
        return "AFTER-HOURS"
    return "CLOSED"


def _median_positive(values: list[float]) -> float | None:
    positive = [value for value in values if math.isfinite(value) and value > 0]
    if len(positive) < 2:
        return None
    return float(statistics.median(positive))


def _time_number(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _close_before(frame: pd.DataFrame, point: datetime, tolerance_minutes: int) -> float | None:
    close = _column(frame.loc[frame.index <= point], "Close").dropna()
    if close.empty:
        return None
    stamp = close.index[-1]
    if point - stamp > timedelta(minutes=tolerance_minutes):
        return None
    return float(close.iloc[-1])


def analyze_ticker(
    ticker: str,
    daily: DailyProfile,
    intraday: pd.DataFrame,
    now: datetime | None = None,
) -> RunnerSnapshot | None:

    if intraday.empty:
        return None
    now_et = (now or datetime.now(UTC)).astimezone(EASTERN)
    clean = _to_eastern_index(intraday)
    required = {name: _column(clean, name) for name in ("Close", "High", "Low", "Volume")}
    if required["Close"].dropna().empty:
        return None
    usable = clean.loc[required["Close"].notna()].copy()
    usable = usable.loc[usable.index <= now_et]
    if usable.empty:
        return None

    latest_time = usable.index[-1]
    latest_date = latest_time.date()
    latest_clock = _time_number(latest_time.time())
    all_dates = sorted({stamp.date() for stamp in usable.index})
    current = usable[usable.index.date == latest_date]
    session_mask = [
        time(4) <= stamp.time().replace(tzinfo=None) <= time(20) for stamp in current.index
    ]
    current = current.loc[session_mask]
    if current.empty:
        return None

    current_close = _column(current, "Close").dropna()
    current_high = _column(current, "High").dropna()
    current_low = _column(current, "Low").dropna()
    current_volume = _column(current, "Volume").fillna(0).clip(lower=0)
    if current_close.empty or current_high.empty or current_low.empty:
        return None

    price = float(current_close.iloc[-1])
    session_volume = int(current_volume.sum())

    previous_cumulative: list[float] = []
    previous_recent: list[float] = []
    recent_start = latest_clock - 15 * 60
    for previous_date in all_dates:
        if previous_date >= latest_date:
            continue
        day = usable[usable.index.date == previous_date]
        clocks = pd.Series([_time_number(stamp.time()) for stamp in day.index], index=day.index)
        day_volume = _column(day, "Volume").fillna(0).clip(lower=0)
        normal_hours = clocks >= _time_number(time(4))
        cumulative_mask = normal_hours & (clocks <= latest_clock)
        recent_mask = normal_hours & (clocks > recent_start) & (clocks <= latest_clock)
        previous_cumulative.append(float(day_volume.loc[cumulative_mask].sum()))
        previous_recent.append(float(day_volume.loc[recent_mask].sum()))

    typical_cumulative = _median_positive(previous_cumulative)
    typical_recent = _median_positive(previous_recent)
    current_clocks = pd.Series(
        [_time_number(stamp.time()) for stamp in current.index], index=current.index
    )
    current_recent = float(current_volume.loc[current_clocks > recent_start].sum())
    relative_volume = session_volume / typical_cumulative if typical_cumulative else None
    recent_relative_volume = current_recent / typical_recent if typical_recent else None

    comparison_5m = _close_before(current, latest_time - timedelta(minutes=5), 8)
    comparison_10m = _close_before(current, latest_time - timedelta(minutes=10), 8)
    comparison_15m = _close_before(current, latest_time - timedelta(minutes=15), 10)
    momentum_5m = (price / comparison_5m - 1) * 100 if comparison_5m else 0.0
    momentum_15m = (price / comparison_15m - 1) * 100 if comparison_15m else 0.0
    momentum_previous_5m = (
        (comparison_5m / comparison_10m - 1) * 100 if comparison_5m and comparison_10m else 0.0
    )
    momentum_acceleration = momentum_5m - momentum_previous_5m
    change_pct = (price / daily.previous_close - 1) * 100
    breakout_pct = (price / daily.previous_high - 1) * 100
    low = float(current_low.min())
    high = float(current_high.max())
    range_position = (price - low) / (high - low) if high > low else 0.5
    range_position = max(0.0, min(1.0, range_position))
    dollar_volume = session_volume * price
    recent_dollar_volume = current_recent * price
    recent_returns = current_close.pct_change().dropna().tail(6) * 100
    intraday_volatility = float(recent_returns.std(ddof=0)) if len(recent_returns) >= 2 else 0.0
    typical_price = (
        _column(current, "High") + _column(current, "Low") + _column(current, "Close")
    ) / 3
    vwap_weights = current_volume.reindex(typical_price.index).fillna(0)
    vwap = (
        float((typical_price.fillna(0) * vwap_weights).sum() / vwap_weights.sum())
        if float(vwap_weights.sum()) > 0
        else price
    )
    vwap_position_pct = (price / vwap - 1) * 100 if vwap > 0 else 0.0
    pullback_from_high_pct = (high - price) / high * 100 if high > 0 else 0.0
    latest_high = float(current_high.iloc[-1])
    latest_low = float(current_low.iloc[-1])
    close_location = (
        (price - latest_low) / (latest_high - latest_low) if latest_high > latest_low else 0.5
    )
    close_location = max(0.0, min(1.0, close_location))
    stale_minutes = max(0.0, (now_et - (latest_time + timedelta(minutes=5))).total_seconds() / 60)
    drawdown_20d = max(0.0, (1 - price / daily.high_20d) * 100) if daily.high_20d > 0 else 0.0
    drawdown_90d = max(0.0, (1 - price / daily.high_90d) * 100) if daily.high_90d > 0 else 0.0
    drawdown_52w = max(0.0, (1 - price / daily.high_52w) * 100) if daily.high_52w > 0 else 0.0
    rebound_from_20d_low = max(0.0, (price / daily.low_20d - 1) * 100) if daily.low_20d > 0 else 0.0
    structure = analyze_market_structure(usable)

    result = score_runner(
        ScoreInput(
            change_pct=change_pct,
            momentum_5m_pct=momentum_5m,
            momentum_15m_pct=momentum_15m,
            relative_volume=relative_volume,
            recent_relative_volume=recent_relative_volume,
            breakout_pct=breakout_pct,
            range_position=range_position,
            dollar_volume=dollar_volume,
            stale_minutes=stale_minutes,
            momentum_previous_5m_pct=momentum_previous_5m,
            momentum_acceleration_pct=momentum_acceleration,
            intraday_volatility_pct=intraday_volatility,
            vwap_position_pct=vwap_position_pct,
            pullback_from_high_pct=pullback_from_high_pct,
            close_location=close_location,
            recent_dollar_volume=recent_dollar_volume,
        )
    )
    risks = list(result.risks)
    if price < 1:
        risks.append("sub-$1 stock")
    if latest_date != now_et.date():
        risks.append("last trading session only")
    risk = assess_risk(
        RiskInput(
            setup_score=result.score,
            price=price,
            change_pct=change_pct,
            momentum_5m_pct=momentum_5m,
            momentum_15m_pct=momentum_15m,
            vwap_position_pct=vwap_position_pct,
            pullback_from_high_pct=pullback_from_high_pct,
            close_location=close_location,
            dollar_volume=dollar_volume,
            recent_dollar_volume=recent_dollar_volume,
            stale_minutes=stale_minutes,
            drawdown_20d_pct=drawdown_20d,
            drawdown_90d_pct=drawdown_90d,
            drawdown_52w_pct=drawdown_52w,
            rebound_from_20d_low_pct=rebound_from_20d_low,
        )
    )
    risks.extend(risk.risk_reasons)

    return RunnerSnapshot(
        ticker=ticker,
        score=result.score,
        stage=result.stage,
        session=_session_name(now_et),
        price=price,
        change_pct=change_pct,
        momentum_5m_pct=momentum_5m,
        momentum_15m_pct=momentum_15m,
        relative_volume=relative_volume,
        recent_relative_volume=recent_relative_volume,
        breakout_pct=breakout_pct,
        range_position=range_position,
        session_volume=session_volume,
        dollar_volume=dollar_volume,
        average_volume=int(daily.average_volume),
        average_dollar_volume=daily.average_dollar_volume,
        quote_time=latest_time.to_pydatetime(),
        stale_minutes=stale_minutes,
        momentum_previous_5m_pct=momentum_previous_5m,
        momentum_acceleration_pct=momentum_acceleration,
        intraday_volatility_pct=intraday_volatility,
        vwap_position_pct=vwap_position_pct,
        pullback_from_high_pct=pullback_from_high_pct,
        close_location=close_location,
        recent_dollar_volume=recent_dollar_volume,
        opening_range_position=structure.features.opening_range_position,
        opening_range_breakout_pct=structure.features.opening_range_breakout_pct,
        support_distance_pct=structure.features.support_distance_pct,
        support_strength=structure.features.support_strength,
        resistance_distance_pct=structure.features.resistance_distance_pct,
        resistance_strength=structure.features.resistance_strength,
        fib_retracement_pct=structure.features.fib_retracement_pct,
        fib_level_distance_pct=structure.features.fib_level_distance_pct,
        structure_available=structure.features.structure_available,
        fibonacci_available=structure.features.fibonacci_available,
        drawdown_20d_pct=drawdown_20d,
        drawdown_90d_pct=drawdown_90d,
        drawdown_52w_pct=drawdown_52w,
        rebound_from_20d_low_pct=rebound_from_20d_low,
        rug_score=risk.rug_score,
        rug_level=risk.rug_level,
        trade_state=risk.trade_state,
        state_reason=risk.state_reason,
        hard_veto=risk.hard_veto,
        crash_candidate=risk.crash_candidate,
        signals=result.signals,
        risks=list(dict.fromkeys(risks)),
    )


class RunnerScanner:
    def __init__(self, provider: MarketDataProvider) -> None:
        self.provider = provider

    def scan(
        self,
        symbols: list[str],
        settings: ScanSettings | None = None,
        progress: ProgressCallback | None = None,
        now: datetime | None = None,
    ) -> ScanResult:
        settings = settings or ScanSettings()
        settings.validate()
        started = datetime.now(UTC)
        now = now or started
        clean_symbols = list(
            dict.fromkeys(normalize_symbol(symbol) for symbol in symbols if symbol.strip())
        )
        warnings: list[str] = []
        if not clean_symbols:
            finished = datetime.now(UTC)
            return ScanResult([], 0, 0, 0, [], ["No symbols were supplied."], started, finished)

        daily_result = self.provider.daily(clean_symbols, progress)
        warnings.extend(daily_result.warnings)
        profiles: list[DailyProfile] = []
        for ticker, frame in daily_result.frames.items():
            profile = build_daily_profile(ticker, frame, now)
            if profile is None:
                continue
            if not settings.min_price <= profile.previous_close <= settings.max_price:
                continue
            if profile.average_volume < settings.min_avg_volume:
                continue
            if profile.average_dollar_volume < settings.min_avg_dollar_volume:
                continue
            previous_drawdown = max(
                (1 - profile.previous_close / profile.high_90d) * 100,
                (1 - profile.previous_close / profile.high_52w) * 100,
            )
            if settings.crash_only and previous_drawdown < settings.crash_drawdown_pct:
                continue
            profiles.append(profile)

        profiles.sort(
            key=lambda item: (item.average_volume, item.average_dollar_volume), reverse=True
        )
        liquid_count = len(profiles)
        if len(profiles) > settings.max_symbols:
            warnings.append(
                f"{len(profiles):,} symbols passed the daily filter; the most active "
                f"{settings.max_symbols:,} were checked intraday. Raise the scan cap "
                "for wider coverage."
            )
            if settings.crash_only:
                profiles = profiles[: settings.max_symbols]
            else:
                crash_profiles = [
                    profile
                    for profile in profiles
                    if max(
                        (1 - profile.previous_close / profile.high_90d) * 100,
                        (1 - profile.previous_close / profile.high_52w) * 100,
                    )
                    >= settings.crash_drawdown_pct
                ]
                reserve = min(len(crash_profiles), max(1, settings.max_symbols // 3))
                selected = crash_profiles[:reserve]
                selected_tickers = {profile.ticker for profile in selected}
                selected.extend(
                    profile for profile in profiles if profile.ticker not in selected_tickers
                )
                profiles = selected[: settings.max_symbols]

        intraday_symbols = [profile.ticker for profile in profiles]
        intraday_result = self.provider.intraday(intraday_symbols, progress)
        warnings.extend(intraday_result.warnings)
        rows: list[RunnerSnapshot] = []
        for profile in profiles:
            frame = intraday_result.frames.get(profile.ticker)
            if frame is None:
                continue
            snapshot = analyze_ticker(profile.ticker, profile, frame, now)
            if snapshot is None:
                continue
            if (
                snapshot.session in {"PRE-MARKET", "REGULAR"}
                and snapshot.stale_minutes > settings.max_stale_minutes
            ):
                continue
            rows.append(snapshot)

        rows.sort(key=lambda item: (item.score, item.dollar_volume), reverse=True)
        all_rows = list(rows)
        rows = rows[: settings.top_n]
        failed = sorted(set(daily_result.failed + intraday_result.failed))
        if failed:
            warnings.append(
                f"No usable data came back for {len(failed):,} symbol(s). Delisted symbols, "
                "Yahoo limits, and temporary quote errors can cause this."
            )
        if getattr(self.provider, "is_sample", False):
            warnings.append("Sample results use fake data and are only for testing the screen.")
        else:
            warnings.append(
                "Market data can be delayed or incomplete. Check a live broker before acting."
            )
        finished = datetime.now(UTC)
        return ScanResult(
            rows=rows,
            requested_symbols=len(clean_symbols),
            liquid_symbols=liquid_count,
            scanned_symbols=len(intraday_result.frames),
            failed_symbols=failed,
            warnings=warnings,
            started_at=started,
            finished_at=finished,
            all_rows=all_rows,
        )
