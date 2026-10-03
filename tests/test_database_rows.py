from __future__ import annotations

import sqlite3

import pytest

from runner_web.database import CursorResult, ResultRow


def _result(sql: str) -> CursorResult:
    return CursorResult(sqlite3.connect(":memory:").execute(sql))


def test_rows_read_by_name_position_and_slice():
    row = _result("SELECT 'AAA' AS ticker, 1.5 AS close, 7 AS volume").fetchone()

    assert row["ticker"] == "AAA" and row[1] == 1.5 and row[1:] == (1.5, 7)
    assert dict(row) == {"ticker": "AAA", "close": 1.5, "volume": 7}
    assert list(row) == ["AAA", 1.5, 7] and len(row) == 3
    with pytest.raises(KeyError):
        row["missing"]


def test_rows_of_one_query_share_their_names_and_lookup():
    rows = _result("SELECT value AS n, value * 2 AS twice FROM json_each('[1,2,3]')").fetchall()

    # One name map per query, not one per row: large reads stay small.
    assert len({id(row._index) for row in rows}) == 1
    assert len({id(row._keys) for row in rows}) == 1
    assert [row["twice"] for row in rows] == [2, 4, 6]
    assert [row["n"] for row in _result("SELECT 5 AS n UNION ALL SELECT 6")] == [5, 6]


def test_a_repeated_column_name_reads_its_last_occurrence():
    row = _result("SELECT 1 AS x, 2 AS x").fetchone()

    assert row["x"] == 2


def test_a_row_built_by_hand_still_works():
    row = ResultRow(["a", "b"], [1, 2])

    assert row["b"] == 2 and row.keys() == ("a", "b")
    with pytest.raises(ValueError):
        ResultRow(["a"], [1, 2])
