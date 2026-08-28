from datetime import UTC, datetime
from pathlib import Path

from runner_web.market_clock import market_clock


def test_market_clock_walks_through_extended_hours() -> None:
    pre = market_clock(datetime(2026, 8, 24, 13, 0, tzinfo=UTC))
    regular = market_clock(datetime(2026, 8, 24, 14, 0, tzinfo=UTC))
    after = market_clock(datetime(2026, 8, 24, 21, 0, tzinfo=UTC))
    overnight = market_clock(datetime(2026, 8, 25, 1, 0, tzinfo=UTC))

    assert (pre["session"], pre["next_label"]) == ("pre", "Regular open")
    assert (regular["session"], regular["next_label"]) == (
        "regular",
        "After-hours",
    )
    assert (after["session"], after["next_label"]) == ("after", "Overnight")
    assert (overnight["session"], overnight["next_label"]) == (
        "overnight",
        "Pre-market",
    )
    assert pre["scanner_active"] is True
    assert overnight["scanner_active"] is False
    assert overnight["countdown_seconds"] == 7 * 60 * 60


def test_market_clock_handles_the_weekend_boundary() -> None:
    saturday = market_clock(datetime(2026, 8, 29, 16, 0, tzinfo=UTC))
    sunday_night = market_clock(datetime(2026, 8, 31, 1, 0, tzinfo=UTC))

    assert saturday["session"] == "closed"
    assert saturday["next_label"] == "Overnight venues"
    assert sunday_night["session"] == "overnight"
    assert sunday_night["next_label"] == "Pre-market"


def test_market_clock_copy_distinguishes_now_from_the_next_session() -> None:
    root = Path(__file__).parents[1]
    template = (root / "web/templates/_market_clock.html").read_text()
    script = (root / "web/static/market-clock.js").read_text()

    assert "{{ market_clock.next_label }} in …" in template
    assert "`${clock.dataset.nextLabel} in ${duration(remaining)}`" in script
