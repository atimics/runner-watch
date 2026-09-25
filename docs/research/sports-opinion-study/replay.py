"""Replay the sports opinion study against the checked-out application.

Run from the repository root: PYTHONPATH=src python docs/research/sports-opinion-study/replay.py
The database exercise uses a new temporary SQLite database.
"""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from runner_web import db, sports


def event_fixture() -> dict:
    return {
        "id": "study-001",
        "name": "Arizona at Colorado (illustrative)",
        "season": {"year": 2026, "slug": "regular-season"},
        "date": "2026-09-23T20:00:00+00:00",
        "status": {"type": {"state": "pre", "completed": False}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "score": "0",
                        "team": {
                            "id": "study-col",
                            "displayName": "Colorado",
                            "abbreviation": "COL",
                        },
                        "records": [{"name": "overall", "summary": "64-86"}],
                    },
                    {
                        "homeAway": "away",
                        "score": "0",
                        "team": {
                            "id": "study-ari",
                            "displayName": "Arizona",
                            "abbreviation": "ARI",
                        },
                        "records": [{"name": "overall", "summary": "90-60"}],
                    },
                ],
                "odds": [
                    {
                        "provider": {"name": "Illustrative book"},
                        "moneyline": {
                            "home": {"close": {"odds": "-105"}},
                            "away": {"close": {"odds": "-115"}},
                        },
                    }
                ],
            }
        ],
    }


def factor_example() -> dict:
    event = sports.normalize_event("mlb", event_fixture())
    assert event is not None
    prediction = sports.predict_event(event)
    form = 65 * ((90 + 8) / (150 + 16) - (64 + 8) / (150 + 16))
    venue = -100 * sports.LEAGUES["mlb"]["home_edge"]
    raw = 50 + form + venue
    clipped = max(18, min(82, raw))
    assert math.isclose(clipped / 100, prediction["away_probability"], abs_tol=0.0000005)
    return {
        "input_kind": "illustrative",
        "target": "ARI to win",
        "away_record": "90-60",
        "home_record": "64-86",
        "baseline_pct": 50,
        "record_delta_pp": round(form, 9),
        "venue_delta_pp": round(venue, 9),
        "clamp_delta_pp": round(clipped - raw, 9),
        "reconstructed_probability_pct": round(clipped, 9),
        "prediction": prediction,
    }


def snapshot_example() -> dict:
    original_path, original_url = db.DATABASE_PATH, db.DATABASE_URL
    original_required = db.REQUIRE_DATABASE_URL
    try:
        with TemporaryDirectory(prefix="sports-opinion-study-") as temporary:
            db.DATABASE_PATH = Path(temporary) / "study.db"
            db.DATABASE_URL = ""
            db.REQUIRE_DATABASE_URL = False
            db.init_db()
            start = datetime(2026, 9, 23, 20, tzinfo=UTC)
            raw = event_fixture()
            event = sports.normalize_event("mlb", raw)
            assert event is not None
            sports.store_events([event], observed_at=start - timedelta(hours=1))

            final_raw = deepcopy(raw)
            final_raw["status"]["type"] = {
                "state": "post",
                "completed": True,
                "shortDetail": "Final",
            }
            final_raw["competitions"][0]["competitors"][0]["score"] = "3"
            final_raw["competitions"][0]["competitors"][1]["score"] = "5"
            final_raw["competitions"][0]["competitors"][1]["records"][0]["summary"] = "91-60"
            final = sports.normalize_event("mlb", final_raw)
            assert final is not None
            sports.store_events([final], observed_at=start + timedelta(hours=3))

            with db.connection() as database:
                rows = database.execute(
                    "SELECT * FROM sports_events WHERE id=?", (event["id"],)
                ).fetchall()
                listed = sports._event_rows(database, rows)[0]["prediction"]
            detail = sports.sports_event(event["id"])
            assert detail is not None
            sealed = detail["prediction"]
            assert listed["observed_at"] > start.isoformat()
            assert sealed["observed_at"] < start.isoformat()
            assert listed["observed_at"] != sealed["observed_at"]
            return {
                "input_kind": "synthetic local database",
                "game_start": start.isoformat(),
                "list_observed_at": listed["observed_at"],
                "detail_observed_at": sealed["observed_at"],
                "list_away_probability": listed["away_probability"],
                "detail_away_probability": sealed["away_probability"],
                "snapshots_match": listed["observed_at"] == sealed["observed_at"],
                "detail_is_sealed": detail["receipt"]["sealed"],
            }
    finally:
        db.DATABASE_PATH, db.DATABASE_URL = original_path, original_url
        db.REQUIRE_DATABASE_URL = original_required


if __name__ == "__main__":
    print(
        json.dumps(
            {"factors": factor_example(), "snapshot_selection": snapshot_example()}, indent=2
        )
    )
