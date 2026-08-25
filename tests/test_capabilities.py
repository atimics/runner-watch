from pathlib import Path

from pytest import MonkeyPatch

from runner_web import db
from runner_web.db import init_db
from runner_web.main import runtime_capabilities


def test_runtime_capabilities_reports_live_modes_without_secrets(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DATABASE_PATH", tmp_path / "capabilities.db")
    init_db()

    result = runtime_capabilities()

    assert result["analysis"]["evidence_gate"] == {
        "mode": "independent_families",
        "version": 2,
        "required_family": "market",
        "threshold": 3,
        "families": ["market", "primary", "news", "crowd"],
    }
    assert result["analysis"]["market_base_rates"]["minimum_samples"] == 20
    assert "sec:current_filings" in result["sources"]
    assert "credential_env" not in result["sources"]["sec:current_filings"]
