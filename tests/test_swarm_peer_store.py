import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runner_swarm.peer_store import (
    ClaimState,
    IngestOutcome,
    PeerClaimStore,
    PeerStoreError,
    PeerStoreLimits,
)
from runner_swarm.protocol import node_id_from_public_key
from runner_swarm.signed_claim import (
    ClaimVerificationError,
    EvidenceReferenceV1,
    RetractionV1,
    RunnerObservationV1,
    SignedClaimV1,
    SourceVersionV1,
    TradeState,
    encode_public_key,
)

NOW = datetime(2026, 8, 30, 16, tzinfo=UTC)
EVIDENCE_ID = "sha256:" + "a" * 64


def signed_observation(
    key: Ed25519PrivateKey,
    *,
    issued_at: datetime = NOW,
    expires_at: datetime | None = None,
    instrument: str = "NASDAQ:PEN",
    supersedes: str | None = None,
) -> SignedClaimV1:
    public_key = encode_public_key(key.public_key())
    claim = RunnerObservationV1(
        issuer_node_id=node_id_from_public_key(public_key),
        issuer_public_key=public_key,
        issued_at=issued_at,
        expires_at=expires_at or issued_at + timedelta(minutes=15),
        instrument=instrument,
        observed_at=issued_at,
        scanner_version="scanner-v1",
        schema_version="runner-v1",
        source_versions=(SourceVersionV1(family="market", source="example.bars", version="v1"),),
        setup_score_milli=72_000,
        trade_state=TradeState.WATCH,
        state_reason="Peer observation for store tests.",
        evidence=(
            EvidenceReferenceV1(
                evidence_id=EVIDENCE_ID,
                family="market",
                source="example.bars",
                observed_at=issued_at,
            ),
        ),
        supersedes_claim_id=supersedes,
    )
    return SignedClaimV1.sign(claim, key)


def signed_retraction(
    key: Ed25519PrivateKey,
    target: str,
    *,
    issued_at: datetime,
    expires_at: datetime | None = None,
) -> SignedClaimV1:
    public_key = encode_public_key(key.public_key())
    return SignedClaimV1.sign(
        RetractionV1(
            issuer_node_id=node_id_from_public_key(public_key),
            issuer_public_key=public_key,
            issued_at=issued_at,
            expires_at=expires_at or issued_at + timedelta(days=1),
            target_claim_id=target,
            reason="The observation used the wrong timestamp.",
        ),
        key,
    )


def test_store_keeps_peer_claims_in_a_dedicated_database(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    signed = signed_observation(key)
    path = tmp_path / "swarm" / "peer.sqlite3"

    with PeerClaimStore(path) as store:
        result = store.ingest_wire(signed.to_wire_bytes(), topic="Alpha/Nasdaq", received_at=NOW)
        current = store.current_claims(topic="alpha/nasdaq", at=NOW)

    assert result.accepted is True
    assert result.state == ClaimState.ACTIVE
    assert current[0].signed_claim == signed
    assert current[0].topic == "alpha/nasdaq"

    with sqlite3.connect(path) as database:
        tables = {
            row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "peer_claims" in tables
    assert "evidence_claims" not in tables
    assert "market_bars" not in tables


def test_replay_is_deduplicated_across_topics_and_restarts(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    signed = signed_observation(key)
    path = tmp_path / "peer.sqlite3"

    with PeerClaimStore(path) as store:
        first = store.ingest(signed, topic="alpha", received_at=NOW)
    with PeerClaimStore(path) as reopened:
        replay = reopened.ingest(signed, topic="risk", received_at=NOW + timedelta(seconds=1))
        events = reopened.audit_events(limit=2)

    assert first.outcome == IngestOutcome.ACCEPTED
    assert replay.outcome == IngestOutcome.DUPLICATE
    assert replay.state == ClaimState.ACTIVE
    assert [event.outcome for event in events] == ["duplicate", "accepted"]


def test_invalid_signature_never_enters_the_store(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    signed = signed_observation(key)
    damaged = signed.model_copy(update={"signature": "A" * 86})
    path = tmp_path / "peer.sqlite3"

    with pytest.raises(ClaimVerificationError):
        PeerClaimStore(path).ingest(damaged, topic="alpha", received_at=NOW)

    assert path.exists() is False


def test_expiry_and_permanent_supersession_control_current_state(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    original = signed_observation(key, expires_at=NOW + timedelta(minutes=30))
    replacement = signed_observation(
        key,
        issued_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
        supersedes=original.claim_id,
    )

    with PeerClaimStore(tmp_path / "peer.sqlite3") as store:
        store.ingest(original, topic="alpha", received_at=NOW)
        store.ingest(replacement, topic="alpha", received_at=NOW + timedelta(minutes=1))

        assert store.get_claim(original.claim_id, at=NOW + timedelta(minutes=2)).state == (
            ClaimState.SUPERSEDED
        )
        current = store.current_claims(at=NOW + timedelta(minutes=2))
        assert [item.signed_claim.claim_id for item in current] == [replacement.claim_id]
        assert store.current_claims(at=NOW + timedelta(minutes=6)) == ()
        assert store.get_claim(replacement.claim_id, at=NOW + timedelta(minutes=6)).state == (
            ClaimState.EXPIRED
        )


def test_retraction_applies_out_of_order_only_while_current(tmp_path: Path) -> None:
    owner = Ed25519PrivateKey.generate()
    stranger = Ed25519PrivateKey.generate()
    original = signed_observation(owner, expires_at=NOW + timedelta(minutes=30))
    retraction = signed_retraction(
        owner,
        original.claim_id,
        issued_at=NOW + timedelta(minutes=1),
        expires_at=NOW + timedelta(minutes=5),
    )
    foreign_retraction = signed_retraction(
        stranger,
        original.claim_id,
        issued_at=NOW + timedelta(minutes=2),
    )

    with PeerClaimStore(tmp_path / "peer.sqlite3") as store:
        store.ingest(retraction, topic="alpha", received_at=NOW + timedelta(minutes=1))
        store.ingest(original, topic="alpha", received_at=NOW + timedelta(minutes=2))
        store.ingest(foreign_retraction, topic="alpha", received_at=NOW + timedelta(minutes=2))

        assert store.get_claim(original.claim_id, at=NOW + timedelta(minutes=3)).state == (
            ClaimState.RETRACTED
        )
        assert store.current_claims(at=NOW + timedelta(minutes=3)) == ()
        current = store.current_claims(at=NOW + timedelta(minutes=6))
        assert [item.signed_claim.claim_id for item in current] == [original.claim_id]


def test_rate_limit_is_per_verified_peer_and_topic(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    limits = PeerStoreLimits(claims_per_window=2, rate_window=timedelta(minutes=1))
    claims = [
        signed_observation(
            key,
            issued_at=NOW + timedelta(seconds=offset),
            instrument=f"NASDAQ:P{offset}",
        )
        for offset in range(3)
    ]

    with PeerClaimStore(tmp_path / "peer.sqlite3", limits=limits) as store:
        outcomes = [
            store.ingest(claim, topic="alpha", received_at=NOW + timedelta(seconds=index)).outcome
            for index, claim in enumerate(claims)
        ]
        other_topic = store.ingest(claims[2], topic="risk", received_at=NOW + timedelta(seconds=2))

    assert outcomes == [
        IngestOutcome.ACCEPTED,
        IngestOutcome.ACCEPTED,
        IngestOutcome.RATE_LIMITED,
    ]
    assert other_topic.outcome == IngestOutcome.ACCEPTED


def test_duplicate_delivery_attempts_consume_peer_topic_rate_limit(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    limits = PeerStoreLimits(claims_per_window=2, rate_window=timedelta(minutes=1))
    first = signed_observation(key)
    second = signed_observation(
        key,
        issued_at=NOW + timedelta(seconds=1),
        instrument="NASDAQ:TWO",
    )

    with PeerClaimStore(tmp_path / "peer.sqlite3", limits=limits) as store:
        outcomes = [
            store.ingest(first, topic="alpha", received_at=NOW).outcome,
            store.ingest(first, topic="alpha", received_at=NOW + timedelta(seconds=1)).outcome,
            store.ingest(first, topic="alpha", received_at=NOW + timedelta(seconds=2)).outcome,
            store.ingest(second, topic="alpha", received_at=NOW + timedelta(seconds=3)).outcome,
        ]

    assert outcomes == [
        IngestOutcome.ACCEPTED,
        IngestOutcome.DUPLICATE,
        IngestOutcome.RATE_LIMITED,
        IngestOutcome.RATE_LIMITED,
    ]


def test_bans_and_local_revocation_never_erase_audit_history(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    node_id = node_id_from_public_key(encode_public_key(key.public_key()))
    first = signed_observation(key)
    second = signed_observation(key, issued_at=NOW + timedelta(minutes=2), instrument="NASDAQ:TWO")

    with PeerClaimStore(tmp_path / "peer.sqlite3") as store:
        store.ingest(first, topic="alpha", received_at=NOW)
        store.ban_peer(
            node_id,
            reason="Local operator block.",
            until=NOW + timedelta(minutes=1),
            at=NOW,
        )
        assert store.current_claims(at=NOW) == ()
        assert store.ingest(second, topic="alpha", received_at=NOW).outcome == IngestOutcome.BANNED
        assert store.is_banned(node_id, at=NOW + timedelta(minutes=2)) is False

        store.revoke_claim(first.claim_id, reason="Failed local evidence policy.", at=NOW)
        assert store.get_claim(first.claim_id, at=NOW).state == ClaimState.REVOKED
        assert store.restore_claim(first.claim_id, at=NOW) is True
        assert store.get_claim(first.claim_id, at=NOW).state == ClaimState.ACTIVE
        actions = {event.action for event in store.audit_events(limit=7)}

    assert {"ingest", "ban_peer", "revoke_claim", "restore_claim"} <= actions


def test_storage_and_audit_are_strictly_bounded(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    limits = PeerStoreLimits(
        claims_per_window=20,
        max_claims=2,
        max_audit_events=3,
        max_control_records=2,
        inactive_retention=timedelta(0),
    )

    with PeerClaimStore(tmp_path / "peer.sqlite3", limits=limits) as store:
        claims = [
            signed_observation(
                key,
                issued_at=NOW + timedelta(seconds=index),
                instrument=f"NASDAQ:B{index}",
            )
            for index in range(4)
        ]
        for index, claim in enumerate(claims):
            store.ingest(claim, topic="alpha", received_at=NOW + timedelta(seconds=index))

        assert len(store.current_claims(at=NOW + timedelta(seconds=4))) == 2
        assert len(store.audit_events(limit=3)) == 3
        assert store.get_claim(claims[0].claim_id, at=NOW + timedelta(seconds=4)) is None

        expired = store.prune(at=NOW + timedelta(minutes=16))
        assert expired.claims == 2
        assert store.current_claims(at=NOW + timedelta(minutes=16)) == ()

        store.revoke_claim("sha256:" + "1" * 64, reason="First control.", at=NOW)
        store.revoke_claim("sha256:" + "2" * 64, reason="Second control.", at=NOW)
        with pytest.raises(PeerStoreError, match="capacity"):
            store.revoke_claim("sha256:" + "3" * 64, reason="Over limit.", at=NOW)
