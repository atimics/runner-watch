"""Daily stories preserve evidence time, breadth and more than one side of a session."""

import copy
import sqlite3
from contextlib import closing

import pytest
from bs4 import BeautifulSoup
from jinja2 import ChainableUndefined, Environment, FileSystemLoader

from runner_web.report_narrative import build_narrative, freeze_story_board, select_stories
from runner_web.report_spotlight import decorate_edition

AS_OF = "2026-09-23T20:10:00+00:00"


def row(ticker="LEAD", **changes):
    return {
        "ticker": ticker,
        "price": 4,
        "quote_time": AS_OF,
        "change_pct": 18,
        "relative_volume": 4,
        "signals": [],
        "risks": [],
        "trade_state": "WATCH",
        **changes,
    }


def report(rows=None, post=True):
    return {
        "report_type": "post_market" if post else "pre_market",
        "report_day": "2026-09-23",
        "as_of": AS_OF,
        "as_of_label": "4:10 PM ET",
        "headline": "Saved headline",
        "summary": "Saved summary",
        "leaders": [],
        "metrics": {
            "story_board": rows or [],
            "closing_breadth": {"candidates": 12, "green": 8, "red": 4},
        },
    }


def test_story_selection_spans_directions_volume_and_more_than_the_watch_board():
    rows = [
        row(),
        row("DOWN", change_pct=-8, relative_volume=1),
        row("BUSY", change_pct=1, relative_volume=9),
        row("ALSO", change_pct=2),
        row("FIFTH", change_pct=3),
        row("SIXTH", change_pct=0),
    ]
    selected = select_stories(rows, AS_OF)
    assert [r["ticker"] for r in selected][:3] == ["LEAD", "DOWN", "BUSY"]
    assert len(selected) == 5
    assert select_stories(list(reversed(rows)), AS_OF) == selected


@pytest.mark.parametrize(
    "changes",
    [
        {"price": 0},
        {"price": float("inf")},
        {"price": float("nan")},
        {"quote_time": "2026-09-23T13:30:00+00:00"},
        {"quote_time": "2026-09-23T20:11:00+00:00"},
        {"quote_time": None},
    ],
)
def test_stale_and_unpriced_names_stay_out_of_the_stories(changes):
    assert select_stories([row(**changes)], AS_OF) == []


def test_saved_company_names_and_rows_survive_later_updates():
    with closing(sqlite3.connect(":memory:")) as database:
        database.row_factory = sqlite3.Row
        database.execute("CREATE TABLE sec_companies(ticker,name,refreshed_at,cik)")
        database.execute("INSERT INTO sec_companies VALUES('LEAD','Lead Systems','2026-09-22',1)")
        database.execute("INSERT INTO sec_companies VALUES('FUTURE','Future Name','2026-09-24',2)")
        rows = [row(), row("FUTURE", change_pct=-2)]
        frozen = freeze_story_board(database, rows, AS_OF, AS_OF)
        before = build_narrative(report(frozen))
        database.execute("UPDATE sec_companies SET name='Changed'")
        rows[0]["price"] = 999
        assert build_narrative(report(frozen)) == before
        assert "Lead Systems (LEAD)" in before["stories"][0]["body"]
        assert frozen[1]["company_name"] == "FUTURE"


def test_blockbuster_label_requires_both_size_and_volume():
    for move, volume, label in [
        (18, 4, "Blockbuster move"),
        (-18, 4, "Blockbuster move"),
        (18, 1, "The main story"),
        (2, 9, "The main story"),
    ]:
        story = build_narrative(report([row(change_pct=move, relative_volume=volume)]))["stories"][
            0
        ]
        assert story["label"] == label


def test_evening_stories_distinguish_scan_move_watch_return_and_target_result():
    saved = report([row(risks=["Thin liquidity"], trade_state="AVOID"), row("NEW", change_pct=-4)])
    saved["leaders"] = [
        {
            **row(),
            "session_return_pct": -2.5,
            "close_is_settled": True,
            "eod_forecast": {"target_price": 5, "status": "miss"},
        }
    ]
    narrative = build_narrative(saved)
    body = narrative["stories"][0]["body"]
    assert "up 18.0%" in body
    assert "settled close stood -2.5% from the opening watch price" in body
    assert "$5.0000 closing target was missed" in body
    assert "Thin liquidity" in body and "AVOID" in body
    assert "joined the story" in narrative["stories"][1]["body"]
    assert narrative["headline"] == "LEAD surges 18.0% as NEW eases 4.0%"


def test_morning_story_keeps_the_target_forward_looking():
    saved = report([row()], post=False)
    saved["leaders"] = [{**row(), "eod_forecast": {"target_price": 5, "status": "hit"}}]
    body = build_narrative(saved)["stories"][0]["body"]
    assert "in view for the close" in body
    assert "was hit" not in body


def test_legacy_evening_uses_close_fields_and_keeps_earlier_signals_out():
    saved = report()
    saved["metrics"].pop("story_board")
    saved["leaders"] = [
        row(
            signals=["Opening signal"],
            change_pct=100,
            close_price=3,
            close_quote_time=AS_OF,
            close_change_pct=-4,
        )
    ]
    narrative = build_narrative(saved)
    assert narrative["headline"] == "LEAD eases 4.0%"
    assert "Opening signal" not in narrative["stories"][0]["body"]


def test_story_rendering_escapes_company_evidence_and_uses_real_company_count():
    from pathlib import Path

    saved = report([row(company_name="<script>bad()</script>"), row("OTHER", change_pct=-4)])
    original = copy.deepcopy(saved)
    decorate_edition(saved)
    template_dir = Path(__file__).resolve().parents[1] / "web/templates"
    env = Environment(
        loader=FileSystemLoader(template_dir), autoescape=True, undefined=ChainableUndefined
    )
    markup = env.from_string(
        '{% from "_report_edition.html" import session_stories %}{{ session_stories(report) }}'
    ).render(report=saved)
    page = BeautifulSoup(markup, "html.parser")
    assert len(page.select(".edition-story")) == 2
    assert not page.select("script")
    assert "<script>bad()</script>" in page.get_text()
    assert saved["metrics"] == original["metrics"]
