from __future__ import annotations

import pytest

from runner_web import deployment_check


@pytest.mark.parametrize(
    ("worker_status", "trainer_status", "expected"),
    [
        ("ok", "ok", True),
        ("stale", "ok", False),
        ("ok", "stopped", False),
    ],
)
def test_background_process_check_requires_both_processes(
    worker_status: str,
    trainer_status: str,
    expected: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        deployment_check,
        "health_status",
        lambda: {
            "worker": {"status": worker_status},
            "trainer": {"status": trainer_status},
        },
    )

    assert deployment_check.background_processes_healthy() is expected


def test_failed_background_process_check_exits_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment_check, "background_processes_healthy", lambda: False)

    with pytest.raises(SystemExit, match="Background process health check failed"):
        deployment_check.main()
