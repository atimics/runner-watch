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


def test_background_process_check_allows_only_stale_progress_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def payload(instance: dict) -> dict:
        return {
            "worker": {"status": "degraded", "detail": {"instances": [instance]}},
            "trainer": {"status": "ok"},
        }

    monkeypatch.setattr(
        deployment_check,
        "health_status",
        lambda: payload(
            {
                "status": "degraded",
                "stale_workers": [{"worker": "scan-collection"}],
                "missing_workers": [],
                "failed_workers": [],
            }
        ),
    )
    assert deployment_check.background_processes_healthy() is True

    monkeypatch.setattr(
        deployment_check,
        "health_status",
        lambda: payload(
            {
                "status": "degraded",
                "stale_workers": [{"worker": "scan-collection"}],
                "missing_workers": ["kol"],
                "failed_workers": [],
            }
        ),
    )
    assert deployment_check.background_processes_healthy() is False

    monkeypatch.setattr(
        deployment_check,
        "health_status",
        lambda: payload({"status": "stale", "stale_workers": []}),
    )
    assert deployment_check.background_processes_healthy() is False


def test_failed_background_process_check_exits_without_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deployment_check, "background_processes_healthy", lambda: False)

    with pytest.raises(SystemExit, match="Background process health check failed"):
        deployment_check.main()
