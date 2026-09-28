from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "database_backup", Path(__file__).resolve().parents[1] / "scripts" / "backup-database.py"
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def volume(identifier="current", **changes):
    return {
        "id": identifier,
        "name": "pg_data",
        "state": "created",
        "attached_machine_id": "machine",
        **changes,
    }


def test_backup_discovers_attached_volumes_and_waits_for_new_snapshot(monkeypatch):
    calls = []
    polls = 0
    old = {"id": "old", "status": "created"}

    def fly(*args):
        nonlocal polls
        calls.append(args)
        if args[:2] == ("volumes", "list"):
            return json.dumps(
                [
                    volume(),
                    volume("other", name="cache"),
                    volume("detached", attached_machine_id=None),
                    volume("gone", state="destroyed"),
                ]
            )
        if args[:3] == ("volumes", "snapshots", "create"):
            return "Scheduled to snapshot volume current"
        polls += 1
        return json.dumps(
            [old]
            if polls <= 2
            else [old, {"id": "new", "status": "pending" if polls == 3 else "created"}]
        )

    pauses = []
    monkeypatch.setattr(backup, "fly", fly)
    monkeypatch.setattr(backup.time, "sleep", pauses.append)
    assert backup.backup_database("test-db") == ["new"]
    assert ("volumes", "snapshots", "create", "current", "--app", "test-db") in calls
    assert len([args for args in calls if "create" in args]) == 1
    assert pauses == [10, 10]


@pytest.mark.parametrize(
    "volumes", [[], [volume(attached_machine_id=None)], [volume(state="destroyed")]]
)
def test_backup_requires_an_attached_database_volume(monkeypatch, volumes):
    monkeypatch.setattr(backup, "fly", lambda *args: json.dumps(volumes))
    with pytest.raises(RuntimeError, match="attached pg_data volume"):
        backup.backup_database("test-db")


@pytest.mark.parametrize("status,error", [("pending", TimeoutError), ("failed", RuntimeError)])
def test_backup_requires_snapshot_completion(monkeypatch, status, error):
    polls = 0

    def fly(*args):
        nonlocal polls
        if args[:2] == ("volumes", "list"):
            return json.dumps([volume()])
        if "create" in args:
            return "Scheduled"
        polls += 1
        return json.dumps([] if polls == 1 else [{"id": "new", "status": status}])

    monkeypatch.setattr(backup, "fly", fly)
    with pytest.raises(error):
        backup.backup_database("test-db", wait_seconds=0)
