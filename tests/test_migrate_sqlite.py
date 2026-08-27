import pytest

from runner_web.migrate_sqlite import _require_empty_target


def test_migration_rejects_a_nonempty_target_even_when_counts_match() -> None:
    with pytest.raises(RuntimeError, match="Target table users is not empty"):
        _require_empty_target("users", 12)


def test_migration_accepts_an_empty_target() -> None:
    _require_empty_target("users", 0)
