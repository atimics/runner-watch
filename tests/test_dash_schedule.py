from datetime import UTC, datetime, timedelta

import pytest

from runner_web import dash_schedule as schedule

NOW = datetime(2026, 11, 14, 21, 50, tzinfo=UTC)


def test_absolute_group_promise_uses_one_utc_instant_and_eastern_label():
    result = schedule.propose({"review_at": "2026-11-14T23:30:00-05:00"}, NOW)
    assert result["review_at"] == "2026-11-15T04:30:00+00:00"
    assert schedule.label(result) == "Nov 14, 2026 at 23:30 EST"


def test_legacy_minutes_resolve_once():
    result = schedule.propose({"review_minutes": 720}, NOW)
    assert result["review_at"] == (NOW + timedelta(minutes=720)).isoformat()


@pytest.mark.parametrize(
    "args",
    [
        {"review_at": "2026-11-15T04:30:00"},
        {"review_at": "2026-11-15T04:30:00Z", "review_minutes": 720},
        {"review_minutes": True},
        {"review_minutes": 29},
        {"review_minutes": 1441},
        {"review_minutes": 60, "review_timezone": "not/a/timezone"},
    ],
)
def test_ambiguous_conflicting_or_unbounded_times_are_rejected(args):
    with pytest.raises(ValueError):
        schedule.propose(args, NOW)


def test_dst_fall_back_preserves_the_explicit_offset():
    now = datetime(2026, 11, 1, 3, tzinfo=UTC)
    first = schedule.propose({"review_at": "2026-11-01T01:30:00-04:00"}, now)
    second = schedule.propose({"review_at": "2026-11-01T01:30:00-05:00"}, now)
    assert first["review_at"] == "2026-11-01T05:30:00+00:00"
    assert second["review_at"] == "2026-11-01T06:30:00+00:00"
    assert schedule.label(first).endswith("01:30 EDT")
    assert schedule.label(second).endswith("01:30 EST")
