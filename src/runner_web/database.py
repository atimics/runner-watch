from __future__ import annotations

import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path
from time import sleep
from typing import Any, ParamSpec, TypeVar

from runner_web.performance import record_database_wait

_P = ParamSpec("_P")
_R = TypeVar("_R")
DATABASE_RETRY_DELAYS = (1, 2, 4, 8, 16)


def retry_database_operation(function: Callable[_P, _R]) -> Callable[_P, _R]:

    @wraps(function)
    def retrying(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        for attempt in range(len(DATABASE_RETRY_DELAYS) + 1):
            try:
                return function(*args, **kwargs)
            except Exception:
                if attempt >= len(DATABASE_RETRY_DELAYS):
                    raise
                sleep(DATABASE_RETRY_DELAYS[attempt])
        raise AssertionError("database retry loop must return or raise")

    return retrying


class ResultRow:
    __slots__ = ("_keys", "_lookup", "_values")

    def __init__(self, keys: Sequence[str], values: Sequence[Any]) -> None:
        self._keys = tuple(keys)
        self._values = tuple(values)
        self._lookup = dict(zip(self._keys, self._values, strict=True))

    def __getitem__(self, key: str | int | slice) -> Any:
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._lookup[key]

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self) -> tuple[str, ...]:
        return self._keys


class CursorResult:
    __slots__ = ("_cursor", "_keys")

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self._keys = tuple(
            getattr(column, "name", column[0]) for column in cursor.description or ()
        )

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def _wrap(self, row: Sequence[Any] | None) -> ResultRow | None:
        return ResultRow(self._keys, row) if row is not None else None

    def fetchone(self) -> ResultRow | None:
        return self._wrap(self._cursor.fetchone())

    def fetchall(self) -> list[ResultRow]:
        return [ResultRow(self._keys, row) for row in self._cursor.fetchall()]

    def __iter__(self) -> Iterator[ResultRow]:
        for row in self._cursor:
            yield ResultRow(self._keys, row)


def _replace_qmarks(statement: str) -> str:
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        char = statement[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    output.append(statement[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            output.append(char)
        elif char == "?":
            output.append("%s")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _replace_named_parameters(statement: str) -> str:
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(statement):
        char = statement[index]
        if quote:
            output.append(char)
            if char == quote:
                if index + 1 < len(statement) and statement[index + 1] == quote:
                    output.append(statement[index + 1])
                    index += 1
                else:
                    quote = None
        elif char in {"'", '"'}:
            quote = char
            output.append(char)
        elif (
            char == ":"
            and (index == 0 or statement[index - 1] != ":")
            and index + 1 < len(statement)
            and (statement[index + 1].isalpha() or statement[index + 1] == "_")
        ):
            end = index + 2
            while end < len(statement) and (statement[end].isalnum() or statement[end] == "_"):
                end += 1
            name = statement[index + 1 : end]
            output.append(f"%({name})s")
            index = end - 1
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _replace_scalar_extrema(statement: str) -> str:

    replacements: list[tuple[int, int, str]] = []
    for match in re.finditer(r"\b(MAX|MIN)\s*\(", statement, flags=re.IGNORECASE):
        depth = 0
        quote: str | None = None
        has_top_level_comma = False
        index = match.end() - 1
        while index < len(statement):
            char = statement[index]
            if quote:
                if char == quote:
                    if index + 1 < len(statement) and statement[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif char in {"'", '"'}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            elif char == "," and depth == 1:
                has_top_level_comma = True
            index += 1
        if has_top_level_comma:
            replacement = "GREATEST" if match.group(1).upper() == "MAX" else "LEAST"
            replacements.append((match.start(1), match.end(1), replacement))
    for start, end, replacement in reversed(replacements):
        statement = statement[:start] + replacement + statement[end:]
    return statement


def postgres_statement(statement: str) -> str:
    sql = statement.strip()
    sql = _replace_scalar_extrema(sql)
    sql = re.sub(r"\bBLOB\b", "BYTEA", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )
    ignored = bool(re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql, flags=re.IGNORECASE))
    if ignored:
        sql = re.sub(
            r"\bINSERT\s+OR\s+IGNORE\b",
            "INSERT",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
        suffix = ";" if sql.endswith(";") else ""
        sql = sql.removesuffix(";").rstrip() + " ON CONFLICT DO NOTHING" + suffix
    return _replace_named_parameters(_replace_qmarks(sql))


def _script_statements(script: str) -> Iterator[str]:
    statement: list[str] = []
    quote: str | None = None
    for char in script:
        if quote:
            statement.append(char)
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            statement.append(char)
            continue
        if char == ";":
            value = "".join(statement).strip()
            if value:
                yield value
            statement = []
            continue
        statement.append(char)
    value = "".join(statement).strip()
    if value:
        yield value


class DatabaseConnection:
    __slots__ = ("backend", "raw")

    def __init__(self, raw: Any, backend: str) -> None:
        self.raw = raw
        self.backend = backend

    def execute(
        self,
        statement: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> CursorResult:
        sql = postgres_statement(statement) if self.backend == "postgres" else statement
        return CursorResult(self.raw.execute(sql, parameters))

    def executemany(
        self,
        statement: str,
        parameters: Iterable[Sequence[Any] | Mapping[str, Any]],
    ) -> CursorResult:
        sql = postgres_statement(statement) if self.backend == "postgres" else statement
        if self.backend == "postgres":
            cursor = self.raw.cursor()
            cursor.executemany(sql, parameters)
            return CursorResult(cursor)
        return CursorResult(self.raw.executemany(sql, parameters))

    def executescript(self, script: str) -> None:
        if self.backend == "sqlite":
            self.raw.executescript(script)
            return
        for statement in _script_statements(script):
            self.execute(statement)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def __enter__(self) -> DatabaseConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


_POOL_LOCK = threading.Lock()
_POSTGRES_POOL: tuple[str, Any] | None = None


def _pool(database_url: str) -> Any:
    global _POSTGRES_POOL
    with _POOL_LOCK:
        if _POSTGRES_POOL and _POSTGRES_POOL[0] == database_url:
            return _POSTGRES_POOL[1]
        if _POSTGRES_POOL:
            _POSTGRES_POOL[1].close()
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=10,
            kwargs={"autocommit": False},
            open=True,
        )
        _POSTGRES_POOL = (database_url, pool)
        return pool


def close_database_pool() -> None:
    global _POSTGRES_POOL
    with _POOL_LOCK:
        if _POSTGRES_POOL:
            _POSTGRES_POOL[1].close()
            _POSTGRES_POOL = None


@contextmanager
def open_database(database_url: str, database_path: Path) -> Iterator[DatabaseConnection]:
    if database_url:
        pool = _pool(database_url)
        started = time.perf_counter()
        with pool.connection() as raw:
            record_database_wait((time.perf_counter() - started) * 1000)
            database = DatabaseConnection(raw, "postgres")
            try:
                yield database
                database.commit()
            except BaseException:
                database.rollback()
                raise
        return

    database_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(database_path, timeout=20)
    raw.row_factory = None
    raw.execute("PRAGMA busy_timeout=20000")
    raw.execute("PRAGMA synchronous=NORMAL")
    raw.execute("PRAGMA temp_store=MEMORY")
    raw.execute("PRAGMA foreign_keys=ON")
    database = DatabaseConnection(raw, "sqlite")
    try:
        yield database
        database.commit()
    except BaseException:
        database.rollback()
        raise
    finally:
        raw.close()


def initialize_sqlite(database_path: Path) -> None:
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with closing(sqlite3.connect(database_path, timeout=20)) as raw, raw as database:
        database.execute("PRAGMA busy_timeout=20000")
        database.execute("PRAGMA journal_mode=WAL")
