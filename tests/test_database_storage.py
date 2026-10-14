from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "database_storage",
    Path(__file__).resolve().parents[1] / "scripts" / "check-database-storage.py",
)
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


def usage(used_gib, free_gib):
    unit = 1024**2
    return (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        f"/dev/vdc {80 * unit} {used_gib * unit} {free_gib * unit} 63% /data\n"
    )


def test_checks_every_running_machine_and_skips_stopped_copies(monkeypatch):
    calls = []

    def fly(*args):
        calls.append(args)
        if args[:2] == ("machine", "list"):
            return json.dumps(
                [
                    {"id": "primary", "state": "started"},
                    {"id": "replica", "state": "started"},
                    {"id": "old", "state": "stopped"},
                ]
            )
        return usage(47, 29)

    monkeypatch.setattr(storage, "fly", fly)
    results = storage.check_storage("test-db")
    assert [row["machine"] for row in results] == ["primary", "replica"]
    assert results[0]["used_percent"] == 61.8
    assert results[0]["free_gib"] == 29
    assert len(calls) == 3
    assert all("old" not in call for call in calls)


@pytest.mark.parametrize("used,free", [(60, 15), (70, 0), (1, 4)])
def test_fails_before_full_disk_or_when_free_space_is_low(monkeypatch, used, free):
    monkeypatch.setattr(
        storage,
        "fly",
        lambda *args: (
            json.dumps([{"id": "primary", "state": "started"}])
            if args[:2] == ("machine", "list")
            else usage(used, free)
        ),
    )
    with pytest.raises(RuntimeError, match="storage needs attention"):
        storage.check_storage("test-db")


@pytest.mark.parametrize("records", [[], [{"id": "old", "state": "stopped"}]])
def test_requires_a_running_database(monkeypatch, records):
    monkeypatch.setattr(storage, "fly", lambda *args: json.dumps(records))
    with pytest.raises(RuntimeError, match="running database"):
        storage.check_storage("test-db")


@pytest.mark.parametrize(
    "output",
    ["", "/dev/vda 100 20 80 20% /", "/dev/vdc 0 0 0 0% /data", "/dev/vdc 100 90 20 90% /data"],
)
def test_rejects_missing_or_invalid_disk_data(output):
    with pytest.raises(ValueError):
        storage.disk_usage(output)


def test_remote_failure_fails_the_check(monkeypatch):
    def fly(*args):
        if args[:2] == ("machine", "list"):
            return json.dumps([{"id": "primary", "state": "started"}])
        raise TimeoutError("SSH timed out")

    monkeypatch.setattr(storage, "fly", fly)
    with pytest.raises(TimeoutError):
        storage.check_storage("test-db")
