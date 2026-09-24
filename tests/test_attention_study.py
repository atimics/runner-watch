from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from runner_web.attention_study import (
    features,
    future_activity,
    index_bars,
    missing_outcome_bounds,
    paired_interval,
    prepare_rows,
    rank_metrics,
    split_days,
)


def bar_window():
    start = datetime(2026, 9, 21, 14, 5, tzinfo=UTC)
    return {
        start + timedelta(minutes=5 * i): {
            "ticker": "ONE",
            "bar_time": (start + timedelta(minutes=5 * i)).isoformat(),
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100,
            "last_collected_at": (start + timedelta(minutes=5 * (i + 1))).isoformat(),
        }
        for i in range(12)
    }


def snapshot(**changes):
    return {
        "snapshot_id": "s",
        "scan_run_id": "run",
        "ticker": "ONE",
        "session": "REGULAR",
        "captured_at": "2026-09-21T14:02:00+00:00",
        "quote_time": "2026-09-21T14:00:00+00:00",
        "stale_minutes": 2,
        "price": 90,
        "change_pct": 3,
        "momentum_5m_pct": 1,
        "momentum_15m_pct": 2,
        "momentum_previous_5m_pct": 0.5,
        "intraday_volatility_pct": 1,
        "relative_volume": 2,
        "recent_relative_volume": 3,
        "dollar_volume": 100_000,
        "recent_dollar_volume": 5_000,
        "average_dollar_volume": 90_000,
        **changes,
    }


@pytest.mark.parametrize("direction", ["up", "down"])
def test_target_is_symmetric_and_starts_from_next_full_bar_open(direction):
    bars = bar_window()
    stamp = sorted(bars)[2]
    bars[stamp]["high" if direction == "up" else "low"] = 104 if direction == "up" else 96
    # A partial decision bar's extreme is outside the target window.
    earlier = datetime(2026, 9, 21, 14, tzinfo=UTC)
    bars[earlier] = {**bars[stamp], "high": 500}
    decision = earlier + timedelta(minutes=2)
    outcome = future_activity(decision, bars)
    assert outcome["target"] == 1
    assert outcome["entry_at"] == "2026-09-21T14:05:00+00:00"
    assert outcome["entry_price"] == 100
    assert outcome["label_end_at"] == "2026-09-21T15:05:00+00:00"
    rows, _ = prepare_rows([snapshot(price=1)], {"ONE": bars})
    assert rows[0]["target"] == outcome["target"]


def test_target_requires_complete_valid_completed_window_for_both_classes():
    decision = datetime(2026, 9, 21, 14, 2, tzinfo=UTC)
    bars = bar_window()
    assert future_activity(decision, bars)["target"] == 0
    first, last = min(bars), max(bars)
    bars[first]["high"] = 105
    incomplete = {key: value for key, value in bars.items() if key != last}
    assert future_activity(decision, incomplete)["target"] is None
    assert future_activity(decision, incomplete)["status"] == "missing_bar"
    bars[last]["last_collected_at"] = last.isoformat()
    assert future_activity(decision, bars)["status"] == "partial_bar"
    bars[last]["low"] = float("nan")
    assert future_activity(decision, bars)["status"] == "invalid_bar"


def test_bar_index_resolves_timezone_aliases_by_last_revision():
    first = next(iter(bar_window().values()))
    revised = {
        **first,
        "bar_time": "2026-09-21T10:05:00-04:00",
        "last_collected_at": "2026-09-22T14:10:00+00:00",
        "high": 106,
    }
    indexed = index_bars([revised, first])
    assert len(indexed["ONE"]) == 1
    assert next(iter(indexed["ONE"].values()))["high"] == 106


def test_features_use_only_market_inputs_and_preserve_direction_symmetry():
    row = snapshot()
    inverse = {
        **row,
        **{
            key: -row[key]
            for key in (
                "change_pct",
                "momentum_5m_pct",
                "momentum_15m_pct",
                "momentum_previous_5m_pct",
            )
        },
    }
    future = {**row, "target": 1, "max_excursion_pct": 99, "return_60m_pct": 1000}
    np.testing.assert_equal(features([row], context=True), features([inverse], context=True))
    np.testing.assert_equal(features([row], context=True), features([future], context=True))
    distant = snapshot(scan_run_id="future", change_pct=100)
    np.testing.assert_equal(
        features([row], context=True)[0], features([row, distant], context=True)[0]
    )


def test_missing_features_remain_missing_and_future_quotes_are_excluded():
    values = features([snapshot(relative_volume=None)], context=True)[0]
    assert np.isnan(values[5])
    assert np.isnan(values[13])
    rows, excluded = prepare_rows([snapshot(quote_time="2026-09-21T15:00:00+00:00")], {})
    assert rows == []
    assert excluded == {"quote_age_outside_0_45_minutes": 1}


def test_split_preserves_dates_and_purges_label_overlap():
    rows = []
    for i in range(10):
        at = datetime(2026, 9, 1, 14, tzinfo=UTC) + timedelta(days=i)
        rows.append(
            {
                "day": at.date().isoformat(),
                "captured_at": at.isoformat(),
                "label_end_at": (at + timedelta(hours=1)).isoformat(),
            }
        )
    original = split_days(rows)
    assert original == {"train": list(range(6)), "validation": [6, 7], "test": [8, 9]}
    rows[5]["label_end_at"] = rows[6]["captured_at"]
    purged = split_days(rows)
    assert purged["train"] == list(range(5))
    assert purged["test"] == original["test"]


def test_ranking_retains_unresolved_top_slots_and_uses_separate_scan_budgets():
    rows = [
        {"scan_run_id": "a", "ticker": "A", "day": "2026-09-01", "target": None},
        {"scan_run_id": "a", "ticker": "B", "day": "2026-09-01", "target": 1},
        {"scan_run_id": "b", "ticker": "C", "day": "2026-09-02", "target": 1},
        {"scan_run_id": "b", "ticker": "D", "day": "2026-09-02", "target": 0},
    ]
    result = rank_metrics(rows, np.asarray([4, 3, 2, 1]), list(range(4)), k=1)
    assert result["selected"] == 2
    assert result["coverage"] == 0.5
    assert result["daily_precision_lower"] == 0.5
    assert result["daily_precision_upper"] == 1
    assert result["precision_given_resolved"] == 1
    assert paired_interval(result, result)["ci95"] == [0, 0]


def test_repeated_ticker_in_a_scan_is_rejected():
    with pytest.raises(ValueError, match="Repeated ticker"):
        prepare_rows([snapshot(), snapshot()], {})


def test_missing_outcome_bounds_cancel_shared_selections():
    rows = [
        {"scan_run_id": "a", "ticker": "A", "day": "2026-09-01", "target": None},
        {"scan_run_id": "a", "ticker": "B", "day": "2026-09-01", "target": 1},
        {"scan_run_id": "a", "ticker": "C", "day": "2026-09-01", "target": None},
    ]
    baseline, candidate = np.asarray([3, 1, 2]), np.asarray([3, 2, 1])
    bounds = missing_outcome_bounds(rows, candidate, baseline, list(range(3)), k=2)
    assert bounds["mean_daily_difference_bounds"] == [0, 0.5]
    identical = missing_outcome_bounds(rows, baseline, baseline, list(range(3)), k=2)
    assert identical["mean_daily_difference_bounds"] == [0, 0]


def test_peer_context_rejects_future_decisions_in_the_same_scan():
    with pytest.raises(ValueError, match="single decision time"):
        features([snapshot(), snapshot(captured_at="2026-09-21T14:10:00+00:00")], context=True)
