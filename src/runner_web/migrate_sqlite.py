from __future__ import annotations

import argparse
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from runner_web import db as schema
from runner_web.database import DatabaseConnection, close_database_pool, open_database


def _quote_identifier(value: str) -> str:
    if not value.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return f'"{value}"'


def _source_tables(source: sqlite3.Connection) -> list[str]:
    rows = source.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='schema_migrations'
        ORDER BY name
        """
    ).fetchall()
    return [str(row[0]) for row in rows]


def _ordered_tables(source: sqlite3.Connection) -> list[str]:
    tables = _source_tables(source)
    table_set = set(tables)
    dependencies = {
        table: {
            str(row[2])
            for row in source.execute(
                f"PRAGMA foreign_key_list({_quote_identifier(table)})"
            ).fetchall()
            if str(row[2]) in table_set and str(row[2]) != table
        }
        for table in tables
    }
    ordered: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(table for table in remaining if not dependencies[table] & remaining)
        if not ready:
            ready = sorted(remaining)
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _source_columns(source: sqlite3.Connection, table: str) -> list[str]:
    return [
        str(row[1])
        for row in source.execute(
            f"PRAGMA table_info({_quote_identifier(table)})"
        ).fetchall()
    ]


def _target_columns(target: DatabaseConnection, table: str) -> set[str]:
    rows = target.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=?
        """,
        (table,),
    ).fetchall()
    return {str(row["column_name"]) for row in rows}


def _batches(rows: Iterable[tuple[Any, ...]], size: int) -> Iterable[list[tuple[Any, ...]]]:
    batch: list[tuple[Any, ...]] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _copy_table(
    source: sqlite3.Connection,
    target: DatabaseConnection,
    table: str,
    batch_size: int,
) -> int:
    source_columns = _source_columns(source, table)
    target_columns = _target_columns(target, table)
    columns = [column for column in source_columns if column in target_columns]
    if not columns:
        return 0
    quoted_table = _quote_identifier(table)
    column_sql = ",".join(_quote_identifier(column) for column in columns)
    placeholders = ",".join("?" for _ in columns)
    select = source.execute(f"SELECT {column_sql} FROM {quoted_table}")
    if target.backend == "postgres":
        copied = 0
        with target.raw.cursor().copy(
            f"COPY {quoted_table}({column_sql}) FROM STDIN"
        ) as copy:
            for row in select:
                copy.write_row(row)
                copied += 1
        target.commit()
        return copied
    insert = (
        f"INSERT INTO {quoted_table}({column_sql}) "
        f"VALUES({placeholders}) ON CONFLICT DO NOTHING"
    )
    copied = 0
    for batch in _batches(select, batch_size):
        target.executemany(insert, batch)
        target.commit()
        copied += len(batch)
    return copied


def _require_empty_target(table: str, target_count: int) -> None:
    if target_count:
        raise RuntimeError(
            f"Target table {table} is not empty ({target_count} rows). "
            "Use --reset-target before migrating."
        )


def _reset_target(database_url: str, source_path: Path) -> None:
    with open_database(database_url, source_path) as target:
        rows = target.execute(
            """
            SELECT tablename FROM pg_tables
            WHERE schemaname=current_schema() ORDER BY tablename
            """
        ).fetchall()
        tables = ",".join(_quote_identifier(str(row["tablename"])) for row in rows)
        if tables:
            target.execute(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")


def _grant_role(target: DatabaseConnection, role: str) -> None:
    quoted_role = _quote_identifier(role)
    target.execute(f"GRANT USAGE,CREATE ON SCHEMA public TO {quoted_role}")
    target.execute(
        f"GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {quoted_role}"
    )
    target.execute(
        f"GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO {quoted_role}"
    )
    target.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO {quoted_role}"
    )
    target.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE,SELECT,UPDATE ON SEQUENCES TO {quoted_role}"
    )


def _reset_sequences(target: DatabaseConnection) -> None:
    rows = target.execute(
        """
        SELECT table_name,column_name
        FROM information_schema.columns
        WHERE table_schema=current_schema() AND position('nextval(' in column_default)=1
        """
    ).fetchall()
    for row in rows:
        table = str(row["table_name"])
        column = str(row["column_name"])
        quoted_table = _quote_identifier(table)
        quoted_column = _quote_identifier(column)
        target.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence(?,?),
                COALESCE(MAX({quoted_column}),1),
                MAX({quoted_column}) IS NOT NULL
            ) FROM {quoted_table}
            """,
            (table, column),
        )


def migrate(
    source_path: Path,
    database_url: str,
    batch_size: int = 1_000,
    *,
    reset_target: bool = False,
    grant_role: str | None = None,
) -> dict[str, int]:
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if not database_url.startswith(("postgres://", "postgresql://")):
        raise ValueError("The target must be a PostgreSQL URL")

    if reset_target:
        _reset_target(database_url, source_path)
    schema.DATABASE_URL = database_url
    schema.init_db()
    source_uri = f"file:{source_path.resolve()}?mode=ro"
    counts: dict[str, int] = {}
    with sqlite3.connect(source_uri, uri=True) as source:
        with open_database(database_url, source_path) as target:
            tables = _ordered_tables(source)
            source_counts = {
                table: int(
                    source.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                    ).fetchone()[0]
                )
                for table in tables
            }
            for table in tables:
                target_count = int(
                    target.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                    ).fetchone()[0]
                )
                _require_empty_target(table, target_count)

            for table in tables:
                source_count = source_counts[table]
                _copy_table(
                    source,
                    target,
                    table,
                    max(1, batch_size),
                )
                target_count = int(
                    target.execute(
                        f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
                    ).fetchone()[0]
                )
                if target_count != source_count:
                    raise RuntimeError(
                        f"Verification failed for {table}: source={source_count}, "
                        f"target={target_count}"
                    )
                counts[table] = target_count
                print(f"{table}: {target_count:,} rows", flush=True)
            _reset_sequences(target)
            if grant_role:
                _grant_role(target, grant_role)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy a Runner Watch SQLite DB to PostgreSQL")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument(
        "--reset-target",
        action="store_true",
        help="Truncate target tables before copying",
    )
    parser.add_argument(
        "--grant-role",
        help="Grant normal application rights on the migrated schema to this PostgreSQL role",
    )
    args = parser.parse_args()
    try:
        counts = migrate(
            args.source,
            args.database_url,
            args.batch_size,
            reset_target=args.reset_target,
            grant_role=args.grant_role,
        )
        print(f"Verified {sum(counts.values()):,} rows across {len(counts)} tables.")
    finally:
        close_database_pool()


if __name__ == "__main__":
    main()
