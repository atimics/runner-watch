from datetime import UTC, datetime
from pathlib import Path

import pytest

from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.identity import private_key_text
from runner_swarm.node_manifest import verify_signed_node_manifest
from runner_swarm.runtime import (
    AttachedSwarmRuntime,
    BootstrapResult,
    SwarmNodeIdentity,
    build_signed_runtime_manifest,
)
from runner_swarm.signed_claim import RunnerObservationV1

NOW = datetime(2026, 8, 30, 17, 12, 45, 123456, tzinfo=UTC)


def _config(tmp_path: Path, **overrides: str) -> SwarmRuntimeConfig:
    return SwarmRuntimeConfig.from_env(
        {
            "SWARM_KEY_PATH": str(tmp_path / "node.key"),
            "SWARM_PUBLIC_URL": "https://node.example",
            **overrides,
        }
    )


def test_runtime_identity_and_manifest_share_one_stable_key(tmp_path: Path) -> None:
    config = _config(tmp_path, SWARM_MODE="attached")
    first_identity = SwarmNodeIdentity.load(config)
    second_identity = SwarmNodeIdentity.load(config)

    signed = build_signed_runtime_manifest(config, first_identity, at=NOW)
    manifest = verify_signed_node_manifest(signed, at=NOW.replace(microsecond=0))

    assert first_identity.node_id == second_identity.node_id == manifest.node_id
    assert manifest.issued_at.microsecond == 0
    assert manifest.endpoints[0].address == "https://node.example/swarm/v1"
    assert {item.name for item in manifest.capabilities} == {
        "claims.publish",
        "claims.receive",
        "discovery.manifest",
    }
    assert manifest.supported_topics == config.topics


def test_runtime_manifest_requires_an_intentional_public_origin(tmp_path: Path) -> None:
    config = SwarmRuntimeConfig.from_env({"SWARM_KEY_PATH": str(tmp_path / "node.key")})
    identity = SwarmNodeIdentity.load(config)

    with pytest.raises(ValueError, match="SWARM_PUBLIC_URL"):
        build_signed_runtime_manifest(config, identity, at=NOW)


def test_runtime_can_load_one_shared_secret_identity(tmp_path: Path) -> None:
    file_identity = SwarmNodeIdentity.load(_config(tmp_path))
    shared_config = _config(
        tmp_path,
        SWARM_KEY_PATH=str(tmp_path / "unused.key"),
        SWARM_NODE_PRIVATE_KEY=private_key_text(file_identity.private_key),
    )

    shared_identity = SwarmNodeIdentity.load(shared_config)

    assert shared_identity.node_id == file_identity.node_id
    assert not shared_config.key_path.exists()


def _scan_row(index: int = 1) -> dict[str, object]:
    return {
        "id": f"snapshot-{index}",
        "ticker": "rati",
        "setup_score": 72.345,
        "rug_score": 18.25,
        "rug_level": "low",
        "trade_state": "armed",
        "state_reason": "Local checks are nearly complete.",
        "hard_veto": 0,
        "signals_json": '["volume expansion", "VWAP reclaim"]',
        "captured_at": NOW.isoformat(),
        "scoring_version": "scanner-3",
        "price": 12.34,
    }


def test_runtime_builds_provider_safe_signed_scan_claim(tmp_path: Path) -> None:
    runtime = AttachedSwarmRuntime.open(_config(tmp_path, SWARM_MODE="attached"))
    try:
        signed = runtime.build_scan_claim(_scan_row(), at=NOW)

        signed.verify(at=NOW)
        assert isinstance(signed.claim, RunnerObservationV1)
        assert signed.claim.instrument == "US:RATI"
        assert signed.claim.setup_score_milli == 72_345
        assert signed.claim.evidence[0].locator == "runner-watch:snapshot:snapshot-1"
        assert b'"price"' not in signed.to_wire_bytes()
        assert b"12.34" not in signed.to_wire_bytes()
    finally:
        runtime.close()


def test_runtime_fans_out_bounded_claims_only_to_negotiated_topics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = AttachedSwarmRuntime.open(
        _config(
            tmp_path,
            SWARM_MODE="attached",
            SWARM_MAX_CLAIMS_PER_SCAN="2",
        )
    )
    runtime.last_bootstrap_results = (
        BootstrapResult(
            origin="https://peer.example",
            peer_node_id="rati-node:" + "1" * 64,
            accepted_topics=runtime.config.topics,
        ),
        BootstrapResult(
            origin="https://ignored.example",
            peer_node_id="rati-node:" + "2" * 64,
            accepted_topics=("another/topic",),
        ),
    )
    deliveries: list[tuple[str, str, str | None]] = []

    def fake_send(
        origin: str,
        signed_claim: object,
        topic: str,
        *,
        expected_peer_node_id: str | None = None,
        at: datetime | None = None,
    ) -> object:
        del signed_claim, at
        deliveries.append((origin, topic, expected_peer_node_id))
        return object()

    monkeypatch.setattr(runtime, "send_claim", fake_send)
    try:
        summary = runtime.publish_scan_rows([_scan_row(1), _scan_row(2), _scan_row(3)], at=NOW)

        assert summary.rows_seen == 3
        assert summary.claims_built == 2
        assert summary.deliveries_succeeded == 2
        assert summary.deliveries_failed == 0
        assert deliveries == [
            (
                "https://peer.example",
                runtime.config.topics[0],
                "rati-node:" + "1" * 64,
            ),
            (
                "https://peer.example",
                runtime.config.topics[0],
                "rati-node:" + "1" * 64,
            ),
        ]
    finally:
        runtime.close()
