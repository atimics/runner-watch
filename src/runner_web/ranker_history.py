from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from runner_watch.market_data import DownloadResult
from runner_watch.models import DailyProfile, ScanSettings
from runner_watch.scanner import RunnerScanner, build_daily_profile
from runner_web.db import connection
from runner_web.outcomes import BARRIER_HORIZON, barrier_outcome, return_pct
from runner_web.product_policy import RANKER_TRAINING
from runner_web.ranker import (
    FEATURE_SCALE,
    FEATURE_SCHEMA_VERSION,
    RETURN_SCALE,
    feature_vector,
)

EASTERN = ZoneInfo("America/New_York")
REPLAY_SCHEMA = "stonks.ranker_historical_replay.v1"
REPLAY_ORIGIN = "historical_replay"
DEFAULT_SOURCE = "yahoo"
DEFAULT_DAYS = 10
DEFAULT_CADENCE_MINUTES = 30
DEFAULT_NEAR_LIVE_MINUTES = 12
BAR_COMPLETION_LAG = timedelta(minutes=5)
LOAD_TICKER_CHUNK_SIZE = 24
REPLAY_MAX_SYMBOLS = 36
REPLAY_INTRADAY_LOOKBACK = timedelta(days=8)
ProgressCallback = Callable[[dict[str, Any]], None]


class ArchivedMarketData:
    """Serve immutable archived frames to the normal scanner at one past time."""

    def __init__(
        self,
        daily_frames: dict[str, pd.DataFrame],
        intraday_frames: dict[str, pd.DataFrame],
        *,
        intraday_start: datetime | None = None,
        intraday_cutoff: datetime | None = None,
    ) -> None:
        self._daily_frames = daily_frames
        self._intraday_frames = intraday_frames
        self._intraday_start = intraday_start
        self._intraday_cutoff = intraday_cutoff

    def daily(self, tickers: list[str], progress: Any = None) -> DownloadResult:
        frames = {
            ticker: self._daily_frames[ticker]
            for ticker in tickers
            if ticker in self._daily_frames
        }
        return DownloadResult(frames, [ticker for ticker in tickers if ticker not in frames], [])

    def intraday(self, tickers: list[str], progress: Any = None) -> DownloadResult:
        frames: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            frame = self._intraday_frames.get(ticker)
            if frame is None:
                continue
            mask = pd.Series(True, index=frame.index)
            if self._intraday_start is not None:
                mask &= frame.index >= self._intraday_start
            if self._intraday_cutoff is not None:
                mask &= frame.index <= self._intraday_cutoff
            frame = frame.loc[mask]
            if not frame.empty:
                frames[ticker] = frame
        return DownloadResult(frames, [ticker for ticker in tickers if ticker not in frames], [])


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    index = pd.to_datetime([row["bar_time"] for row in rows], utc=True)
    return pd.DataFrame(
        {
            "Open": [row.get("open") for row in rows],
            "High": [row.get("high") for row in rows],
            "Low": [row.get("low") for row in rows],
            "Close": [row.get("close") for row in rows],
            "Volume": [row.get("volume") for row in rows],
        },
        index=index,
    ).sort_index()


def _frames(rows: list[Any]) -> dict[str, pd.DataFrame]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        row = dict(raw)
        grouped.setdefault(str(row["ticker"]), []).append(row)
    return {ticker: _frame(values) for ticker, values in grouped.items()}


def _select_source(start_at: datetime, end_at: datetime, requested: str) -> str | None:
    if requested != "auto":
        return requested
    with connection() as database:
        row = database.execute(
            """
            SELECT source,COUNT(*) AS bars FROM market_bars
            WHERE interval='5m' AND bar_time>=? AND bar_time<=?
            GROUP BY source ORDER BY bars DESC,source LIMIT 1
            """,
            (start_at.isoformat(), end_at.isoformat()),
        ).fetchone()
    return str(row["source"]) if row else None


def _load_interval_frames(
    source: str,
    interval: str,
    start_at: datetime,
    end_at: datetime,
    progress: ProgressCallback | None = None,
) -> dict[str, pd.DataFrame]:
    if progress:
        progress(
            {
                "phase": "historical_backfill",
                "stage": f"discovering_{interval}_symbols",
            }
        )
    with connection() as database:
        ticker_rows = database.execute(
            """
            SELECT DISTINCT ticker FROM market_bars
            WHERE source=? AND interval=? AND bar_time>=? AND bar_time<=?
            ORDER BY ticker
            """,
            (source, interval, start_at.isoformat(), end_at.isoformat()),
        ).fetchall()
        tickers = [str(row["ticker"]) for row in ticker_rows]
        frames: dict[str, pd.DataFrame] = {}
        rows_loaded = 0
        for offset in range(0, len(tickers), LOAD_TICKER_CHUNK_SIZE):
            batch = tickers[offset : offset + LOAD_TICKER_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = database.execute(
                f"""
                SELECT ticker,bar_time,open,high,low,close,volume FROM market_bars
                WHERE source=? AND ticker IN ({placeholders}) AND interval=?
                  AND bar_time>=? AND bar_time<=?
                ORDER BY ticker,bar_time
                """,  # noqa: S608 - placeholders are generated above
                (
                    source,
                    *batch,
                    interval,
                    start_at.isoformat(),
                    end_at.isoformat(),
                ),
            ).fetchall()
            frames.update(_frames(rows))
            rows_loaded += len(rows)
            if progress:
                progress(
                    {
                        "phase": "historical_backfill",
                        "stage": f"loading_{interval}_bars",
                        "tickers": len(tickers),
                        "tickers_loaded": min(offset + len(batch), len(tickers)),
                        "rows_loaded": rows_loaded,
                    }
                )
    return frames


def _load_frames(
    source: str,
    start_at: datetime,
    end_at: datetime,
    progress: ProgressCallback | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    daily_frames = _load_interval_frames(
        source,
        "1d",
        start_at - timedelta(days=370),
        end_at,
        progress,
    )
    intraday_frames = _load_interval_frames(
        source,
        "5m",
        start_at - timedelta(days=8),
        end_at + BARRIER_HORIZON + timedelta(minutes=10),
        progress,
    )
    return daily_frames, intraday_frames


def _complete_group_times() -> list[datetime]:
    with connection() as database:
        rows = database.execute(
            """
            SELECT MIN(captured_at) AS captured_at
            FROM ranker_training_examples
            WHERE feature_schema_version=?
              AND barrier_label IN ('down','timeout','up')
            GROUP BY scan_run_id
            HAVING COUNT(*)=MAX(expected_candidates) AND COUNT(*)>=2
            ORDER BY captured_at
            """,
            (FEATURE_SCHEMA_VERSION,),
        ).fetchall()
    output: list[datetime] = []
    for row in rows:
        try:
            output.append(_as_utc(datetime.fromisoformat(str(row["captured_at"]))))
        except ValueError:
            continue
    return output


def _replay_times(
    frames: dict[str, pd.DataFrame],
    start_at: datetime,
    end_at: datetime,
    cadence_minutes: int,
) -> list[datetime]:
    sessions: set[date] = set()
    for frame in frames.values():
        sessions.update(stamp.tz_convert(EASTERN).date() for stamp in frame.index)
    replay_times: list[datetime] = []
    for session in sorted(sessions):
        point = datetime.combine(session, time(4, 5), tzinfo=EASTERN)
        final = datetime.combine(session, time(19, 5), tzinfo=EASTERN)
        while point <= final:
            point_utc = point.astimezone(UTC)
            if start_at <= point_utc <= end_at:
                replay_times.append(point_utc)
            point += timedelta(minutes=cadence_minutes)
    return replay_times


def _session_symbols(frames: dict[str, pd.DataFrame], replay_at: datetime) -> list[str]:
    return sorted(
        ticker
        for ticker, frame in frames.items()
        if _known_on_session(frame, replay_at)
    )


def _known_on_session(frame: pd.DataFrame, replay_at: datetime) -> bool:
    known_index = frame.index[frame.index <= replay_at - BAR_COMPLETION_LAG]
    return bool(
        len(known_index)
        and known_index[-1].tz_convert(EASTERN).date()
        == replay_at.astimezone(EASTERN).date()
    )


def _select_replay_symbols(
    daily_frames: dict[str, pd.DataFrame],
    symbols: list[str],
    settings: ScanSettings,
    replay_at: datetime,
) -> list[str]:
    profiles: list[DailyProfile] = []
    for ticker in symbols:
        frame = daily_frames.get(ticker)
        if frame is None:
            continue
        profile = build_daily_profile(ticker, frame, replay_at)
        if profile is None:
            continue
        if not settings.min_price <= profile.previous_close <= settings.max_price:
            continue
        if profile.average_volume < settings.min_avg_volume:
            continue
        if profile.average_dollar_volume < settings.min_avg_dollar_volume:
            continue
        profiles.append(profile)
    profiles.sort(
        key=lambda item: (item.average_volume, item.average_dollar_volume),
        reverse=True,
    )
    if len(profiles) <= settings.max_symbols:
        return [profile.ticker for profile in profiles]
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
    return [profile.ticker for profile in selected[: settings.max_symbols]]


def _bar_tuples(frame: pd.DataFrame) -> list[tuple[datetime, float, float, float]]:
    output: list[tuple[datetime, float, float, float]] = []
    for stamp, row in frame.iterrows():
        try:
            high = float(row["High"])
            low = float(row["Low"])
            close = float(row["Close"])
        except (TypeError, ValueError):
            continue
        output.append((stamp.to_pydatetime().astimezone(UTC), high, low, close))
    return output


def _return_at_horizon(
    bars: list[tuple[datetime, float, float, float]],
    replay_at: datetime,
    base_price: float,
) -> float | None:
    target = replay_at + BARRIER_HORIZON
    nearby = [
        (abs((stamp - target).total_seconds()), close)
        for stamp, _high, _low, close in bars
        if abs((stamp - target).total_seconds()) <= 10 * 60
    ]
    if not nearby:
        return None
    return return_pct(base_price, min(nearby, key=lambda item: item[0])[1])


def _group_id(source: str, replay_at: datetime) -> str:
    identity = hashlib.sha256(
        f"{REPLAY_SCHEMA}|{FEATURE_SCHEMA_VERSION}|{source}|{replay_at.isoformat()}".encode()
    ).hexdigest()[:24]
    return f"history-{identity}"


def _write_group(
    source: str,
    replay_at: datetime,
    rows: list[dict[str, Any]],
    cadence_minutes: int,
) -> int:
    group_id = _group_id(source, replay_at)
    provenance = json.dumps(
        {
            "schema": REPLAY_SCHEMA,
            "market_bar_source": source,
            "replay_at": replay_at.isoformat(),
            "cadence_minutes": cadence_minutes,
            "bar_completion_lag_minutes": int(BAR_COMPLETION_LAG.total_seconds() / 60),
            "universe": "symbols_with_archived_intraday_bars",
            "cohort_max_symbols": REPLAY_MAX_SYMBOLS,
            "cohort_selection": "point_in_time_daily_liquidity_with_crash_reserve",
            "point_in_time_features": True,
            "limitations": [
                "historical_symbol_availability_may_have_survivorship_bias",
                "catalyst_and_issuer_features_are_marked_missing",
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    with connection() as database:
        database.execute(
            "DELETE FROM ranker_training_examples WHERE scan_run_id=? AND training_origin=?",
            (group_id, REPLAY_ORIGIN),
        )
        database.executemany(
            """
            INSERT INTO ranker_training_examples(
                snapshot_id,scan_run_id,ticker,feature_schema_version,
                expected_candidates,captured_at,feature_vector_json,
                baseline_score_milli,barrier_label,outcome_return_bp,labeled_at,
                training_origin,provenance_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    f"{group_id}-{hashlib.sha256(str(row['ticker']).encode()).hexdigest()[:12]}",
                    group_id,
                    str(row["ticker"]),
                    FEATURE_SCHEMA_VERSION,
                    len(rows),
                    replay_at.isoformat(),
                    json.dumps(feature_vector(row), separators=(",", ":")),
                    int(round(float(row["score"]) * FEATURE_SCALE)),
                    str(row["barrier_label"]),
                    int(round(float(row.get("outcome_return_pct") or 0) * RETURN_SCALE)),
                    datetime.now(UTC).isoformat(),
                    REPLAY_ORIGIN,
                    provenance,
                )
                for row in rows
            ],
        )
    return len(rows)


def backfill_historical_training(
    *,
    days: int = DEFAULT_DAYS,
    cadence_minutes: int = DEFAULT_CADENCE_MINUTES,
    target_groups: int = RANKER_TRAINING.maximum_groups,
    source: str = DEFAULT_SOURCE,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    near_live_minutes: int = DEFAULT_NEAR_LIVE_MINUTES,
    dry_run: bool = False,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Rebuild compact point-in-time groups from already archived market bars."""

    if days < 1:
        raise ValueError("days must be at least 1")
    if cadence_minutes < 5 or cadence_minutes > 240:
        raise ValueError("cadence_minutes must be between 5 and 240")
    if target_groups < 1:
        raise ValueError("target_groups must be at least 1")
    end = _as_utc(end_at or datetime.now(UTC) - BARRIER_HORIZON)
    start = _as_utc(start_at or end - timedelta(days=days))
    if start > end:
        raise ValueError("start_at must be before end_at")

    selected_source = _select_source(start, end + BARRIER_HORIZON, source)
    existing_times = _complete_group_times()
    existing_groups = len(existing_times)
    if existing_groups >= target_groups:
        return {
            "enabled": True,
            "source": selected_source,
            "groups_before": existing_groups,
            "groups_written": 0,
            "rows_written": 0,
            "groups_after": existing_groups,
            "reason": "target_already_met",
            "dry_run": dry_run,
        }
    if selected_source is None:
        return {
            "enabled": True,
            "source": None,
            "groups_before": existing_groups,
            "groups_written": 0,
            "rows_written": 0,
            "groups_after": existing_groups,
            "reason": "no_archived_intraday_source",
            "dry_run": dry_run,
        }

    daily_frames, intraday_frames = _load_frames(selected_source, start, end, progress)
    points = _replay_times(intraday_frames, start, end, cadence_minutes)
    tolerance = timedelta(minutes=max(0, near_live_minutes))
    settings = ScanSettings(
        min_price=0.20,
        max_price=5.00,
        min_avg_volume=100_000,
        min_avg_dollar_volume=250_000,
        max_symbols=REPLAY_MAX_SYMBOLS,
        top_n=REPLAY_MAX_SYMBOLS,
        crash_only=False,
    )
    session_selections: dict[date, list[str]] = {}
    groups_written = 0
    rows_written = 0
    skipped_near_live = 0
    skipped_incomplete = 0
    for position, replay_at in enumerate(points, start=1):
        if progress:
            progress(
                {
                    "phase": "historical_backfill",
                    "point": position,
                    "points": len(points),
                    "groups_written": groups_written,
                    "rows_written": rows_written,
                    "replay_at": replay_at.isoformat(),
                }
            )
        if any(abs(replay_at - existing) <= tolerance for existing in existing_times):
            skipped_near_live += 1
            continue
        session = replay_at.astimezone(EASTERN).date()
        if session not in session_selections:
            selection_at = datetime.combine(session, time(19, 5), tzinfo=EASTERN).astimezone(
                UTC
            )
            session_selections[session] = _select_replay_symbols(
                daily_frames,
                _session_symbols(intraday_frames, selection_at),
                settings,
                replay_at,
            )
        symbols = [
            ticker
            for ticker in session_selections[session]
            if _known_on_session(intraday_frames[ticker], replay_at)
        ]
        if len(symbols) < 2:
            skipped_incomplete += 1
            continue
        scan = RunnerScanner(
            ArchivedMarketData(
                daily_frames,
                intraday_frames,
                intraday_start=replay_at - REPLAY_INTRADAY_LOOKBACK,
                intraday_cutoff=replay_at - BAR_COMPLETION_LAG,
            )
        ).scan(
            symbols,
            settings,
            now=replay_at,
        )
        candidates = scan.all_rows or scan.rows
        labeled: list[dict[str, Any]] = []
        for candidate in candidates:
            bars = _bar_tuples(intraday_frames.get(candidate.ticker, pd.DataFrame()))
            outcome = barrier_outcome(bars, replay_at, candidate.price)
            if outcome is None:
                break
            row = candidate.to_dict()
            row.update(
                {
                    "scan_mode": "penny",
                    "catalyst_score": None,
                    "catalyst_sentiment": None,
                    "issuer_risk_json": json.dumps({"issuer_data_available": False}),
                    "barrier_label": outcome["barrier_label"],
                    "outcome_return_pct": _return_at_horizon(
                        bars,
                        replay_at,
                        candidate.price,
                    ),
                }
            )
            labeled.append(row)
        if len(labeled) != len(candidates) or len(labeled) < 2:
            skipped_incomplete += 1
            continue
        written = len(labeled) if dry_run else _write_group(
            selected_source,
            replay_at,
            labeled,
            cadence_minutes,
        )
        groups_written += 1
        rows_written += written
        existing_times.append(replay_at)
        if existing_groups + groups_written >= target_groups:
            break

    return {
        "enabled": True,
        "source": selected_source,
        "replay_schema": REPLAY_SCHEMA,
        "start_at": start.isoformat(),
        "end_at": end.isoformat(),
        "cadence_minutes": cadence_minutes,
        "groups_before": existing_groups,
        "groups_written": groups_written,
        "rows_written": rows_written,
        "groups_after": existing_groups + groups_written,
        "points_considered": len(points),
        "skipped_near_live": skipped_near_live,
        "skipped_incomplete": skipped_incomplete,
        "target_groups": target_groups,
        "cohort_max_symbols": REPLAY_MAX_SYMBOLS,
        "dry_run": dry_run,
        "limitations": [
            "historical_symbol_availability_may_have_survivorship_bias",
            "catalyst_and_issuer_features_are_marked_missing",
        ],
    }
