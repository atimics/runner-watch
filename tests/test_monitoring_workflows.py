"""Keep frequent health probes separate from the full production browser sweep."""

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).parents[1] / ".github" / "workflows"


def _workflow(name: str) -> dict:
    # BaseLoader preserves GitHub's `on` key instead of treating it as YAML 1.1 true.
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def test_five_minute_uptime_preserves_all_http_probes_without_a_browser() -> None:
    workflow = _workflow("uptime.yml")
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["on"]["schedule"] == [{"cron": "*/5 * * * *"}]
    jobs = workflow["jobs"]
    assert set(jobs) == {"workers", "data", "monitor"}
    assert jobs["workers"]["steps"][0]["run"] == (
        "curl --fail --silent --show-error --max-time 15 --retry 2 --retry-delay 5 "
        "https://runners.rati.chat/health/workers"
    )
    assert jobs["data"]["strategy"]["fail-fast"] == "false"
    assert jobs["data"]["strategy"]["matrix"]["origin"] == [
        "https://runners.rati.chat",
        "https://sports.rati.chat",
    ]
    assert jobs["data"]["steps"][0]["run"] == (
        "curl --fail --silent --show-error --max-time 15 --retry 2 --retry-delay 5 "
        '"${{ matrix.origin }}/health/data"'
    )
    assert [step["run"] for step in jobs["monitor"]["steps"] if "run" in step] == [
        "scripts/smoke-production https://runners.rati.chat",
        "scripts/smoke-production https://sports.rati.chat",
    ]
    for job in jobs.values():
        assert "if" not in job
        for step in job["steps"]:
            assert "if" not in step
            assert "test-live-screens" not in step.get("run", "")
            assert "setup-uv" not in step.get("uses", "")


def test_hourly_screens_keep_manual_route_and_failure_evidence() -> None:
    workflow = _workflow("production-screens.yml")
    # No push, workflow_run or deployment_status trigger: Fly already covers deploys.
    assert set(workflow["on"]) == {"schedule", "workflow_dispatch"}
    assert workflow["on"]["schedule"] == [{"cron": "17 * * * *"}]
    steps = workflow["jobs"]["screens"]["steps"]
    assert any(step.get("uses", "").startswith("astral-sh/setup-uv@") for step in steps)
    render_steps = [step for step in steps if step.get("run") == "scripts/test-live-screens"]
    assert len(render_steps) == 1
    # Use the same full suite and its strict defaults; don't suppress failures.
    assert "env" not in render_steps[0]
    assert "if" not in render_steps[0]
    assert "continue-on-error" not in render_steps[0]
    artifact = next(step for step in steps if step.get("name") == "Save failure screenshots")
    assert artifact["if"] == "failure()"
    assert artifact["with"]["path"] == "test-results/live-screens"
    assert artifact["uses"].startswith("actions/upload-artifact@")


def test_monitoring_workflows_cannot_cancel_each_other() -> None:
    uptime = _workflow("uptime.yml")
    screens = _workflow("production-screens.yml")
    assert uptime["concurrency"]["group"] != screens["concurrency"]["group"]
    for workflow in (uptime, screens):
        assert workflow["permissions"] == {"contents": "read"}
        assert workflow["concurrency"]["cancel-in-progress"] == "false"
        for job in workflow["jobs"].values():
            assert 0 < int(job["timeout-minutes"]) <= 4


def test_post_deploy_screens_remain_a_single_rollback_gate() -> None:
    deploy = _workflow("fly.yml")["jobs"]["deploy"]
    assert deploy["if"] == "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
    steps = deploy["steps"]
    render_steps = [step for step in steps if step.get("run") == "scripts/test-live-screens"]
    assert len(render_steps) == 1
    screens = render_steps[0]
    assert screens["id"] == "screens"
    assert screens["if"] == "steps.deploy.outcome == 'success' && steps.smoke.outcome == 'success'"
    assert screens["env"] == {"LIVE_SCREEN_FAILURE_MS": "5000", "LIVE_SCREEN_RETRIES": "4"}
    for name in ("Roll back failed release", "Report failed release"):
        step = next(step for step in steps if step.get("name") == name)
        assert "steps.screens.outcome == 'failure'" in step["if"]
    artifact = next(step for step in steps if step.get("name") == "Save failure screenshots")
    assert artifact["if"] == "always() && steps.screens.outcome == 'failure'"
    assert artifact["with"]["path"] == "test-results/live-screens"
