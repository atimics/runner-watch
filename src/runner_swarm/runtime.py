"""Runtime assembly shared by solo, cloud, and alpha-pack trader nodes."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter

from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.identity import load_or_create_node_key, private_key_from_text
from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    SignedNodeManifest,
    VersionedDeclaration,
    sign_node_manifest,
)
from runner_swarm.peer_store import IngestOutcome, PeerClaimStore, PeerStoreLimits
from runner_swarm.protocol import (
    canonical_json_bytes,
    content_id,
    node_id_from_public_key,
    normalize_utc,
    public_key_text,
)
from runner_swarm.signed_claim import (
    EvidenceReferenceV1,
    RiskLevel,
    RiskVetoV1,
    RunnerObservationV1,
    SignedClaimV1,
    SourceVersionV1,
    TradeState,
)
from runner_swarm.transport import (
    ClaimExchangeReceipt,
    PeerClaimRejected,
    ReceivedPeerClaim,
    SwarmTransport,
    create_swarm_router,
    fetch_signed_manifest,
    negotiate_with_peer,
    post_claim_to_peer,
)


@dataclass(frozen=True, slots=True)
class SwarmNodeIdentity:
    """One stable local identity that can sign every swarm contract."""

    private_key: Ed25519PrivateKey
    public_key: str
    node_id: str

    @classmethod
    def load(cls, config: SwarmRuntimeConfig) -> SwarmNodeIdentity:
        private_key = (
            private_key_from_text(config.node_private_key_text)
            if config.node_private_key_text is not None
            else load_or_create_node_key(config.key_path)
        )
        public_key = public_key_text(private_key)
        return cls(
            private_key=private_key,
            public_key=public_key,
            node_id=node_id_from_public_key(public_key),
        )


def build_signed_runtime_manifest(
    config: SwarmRuntimeConfig,
    identity: SwarmNodeIdentity,
    *,
    at: datetime | None = None,
) -> SignedNodeManifest:
    """Build the short-lived public discovery record for an attached node."""

    if config.public_url is None:
        raise ValueError("SWARM_PUBLIC_URL is required to publish a swarm manifest")
    issued_at = (at or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    manifest = NodeManifest(
        node_id=identity.node_id,
        public_key=identity.public_key,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(seconds=config.manifest_ttl_seconds),
        software_name="runner-watch",
        software_version=config.software_version,
        capabilities=(
            VersionedDeclaration(name="claims.publish", version="1.0.0"),
            VersionedDeclaration(name="claims.receive", version="1.0.0"),
            VersionedDeclaration(name="discovery.manifest", version="1.0.0"),
        ),
        endpoints=(NodeEndpoint(transport="https", address=f"{config.public_url}/swarm/v1"),),
        schema_versions=(
            VersionedDeclaration(name="rati.alpha_pack", version="1.0.0"),
            VersionedDeclaration(name="rati.signed_claim", version="1.0.0"),
        ),
        supported_topics=config.topics,
    )
    return sign_node_manifest(manifest, identity.private_key)


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """One local view of a configured bootstrap attempt."""

    origin: str
    peer_node_id: str | None
    accepted_topics: tuple[str, ...]
    error: str | None = None

    @property
    def connected(self) -> bool:
        return self.peer_node_id is not None and self.error is None


@dataclass(frozen=True, slots=True)
class ClaimPublishSummary:
    """Bounded result for one local scan fan-out."""

    rows_seen: int
    claims_built: int
    deliveries_succeeded: int
    deliveries_failed: int

    def as_dict(self) -> dict[str, int]:
        return {
            "rows_seen": self.rows_seen,
            "claims_built": self.claims_built,
            "deliveries_succeeded": self.deliveries_succeeded,
            "deliveries_failed": self.deliveries_failed,
        }


def _row_time(value: object, fallback: datetime) -> datetime:
    if not value:
        return fallback
    if isinstance(value, datetime):
        return normalize_utc(value, field_name="scan captured_at")
    text = str(value).strip().replace("Z", "+00:00")
    return normalize_utc(datetime.fromisoformat(text), field_name="scan captured_at")


def _score_milli(value: object) -> int:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("scan score must be finite")
    return max(0, min(100_000, round(number * 1_000)))


def _signals(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = ()
    if not isinstance(value, (list, tuple)):
        return ()
    cleaned = (str(item).strip()[:120] for item in value if item is not None and str(item).strip())
    return tuple(dict.fromkeys(cleaned))[:32]


class AttachedSwarmRuntime:
    """Assemble discovery, exchange, and the isolated peer-claim trust boundary."""

    def __init__(
        self,
        config: SwarmRuntimeConfig,
        identity: SwarmNodeIdentity,
        signed_manifest: SignedNodeManifest,
        peer_store: PeerClaimStore,
    ) -> None:
        if not config.attached:
            raise ValueError("AttachedSwarmRuntime requires SWARM_MODE=attached")
        self.config = config
        self.identity = identity
        self.peer_store = peer_store
        self.router: APIRouter = create_swarm_router(
            signed_manifest,
            receive_claim=self.receive_peer_claim,
            accepted_claim_schema_versions=frozenset({"runner-v1"}),
        )
        self.transport: SwarmTransport = self.router.swarm_transport  # type: ignore[attr-defined]
        self.last_bootstrap_results: tuple[BootstrapResult, ...] = ()

    @classmethod
    def open(cls, config: SwarmRuntimeConfig) -> AttachedSwarmRuntime:
        identity = SwarmNodeIdentity.load(config)
        signed_manifest = build_signed_runtime_manifest(config, identity)
        peer_store = PeerClaimStore(
            config.peer_store_path,
            limits=PeerStoreLimits(
                claims_per_window=config.peer_rate_limit_per_minute,
            ),
        )
        return cls(config, identity, signed_manifest, peer_store)

    def receive_peer_claim(self, received: ReceivedPeerClaim) -> bool:
        """Persist a verified claim as peer context, never as provider evidence."""

        result = self.peer_store.ingest(
            received.signed_claim,
            topic=received.topic,
            received_at=received.received_at,
        )
        if result.outcome == IngestOutcome.BANNED:
            raise PeerClaimRejected("Peer is locally banned", status_code=403)
        if result.outcome == IngestOutcome.RATE_LIMITED:
            raise PeerClaimRejected("Peer claim rate limit exceeded", status_code=429)
        return result.outcome == IngestOutcome.ACCEPTED

    def renew_manifest(self, *, at: datetime | None = None) -> SignedNodeManifest:
        signed_manifest = build_signed_runtime_manifest(
            self.config,
            self.identity,
            at=at,
        )
        self.transport.replace_local_manifest(signed_manifest)
        return signed_manifest

    def refresh_bootstraps(self, *, at: datetime | None = None) -> tuple[BootstrapResult, ...]:
        """Discover and negotiate every configured seed without trusting membership."""

        results: list[BootstrapResult] = []
        for origin in self.config.bootstrap_urls:
            try:
                peer_manifest = fetch_signed_manifest(
                    origin,
                    at=at,
                    allow_private_addresses=self.config.allow_private_bootstrap,
                )
                response = negotiate_with_peer(
                    origin,
                    self.transport.signed_manifest,
                    self.config.topics,
                    expected_peer_node_id=peer_manifest.manifest.node_id,
                    at=at,
                    allow_private_addresses=self.config.allow_private_bootstrap,
                )
                results.append(
                    BootstrapResult(
                        origin=origin,
                        peer_node_id=response.local_node_id,
                        accepted_topics=response.accepted_topics,
                    )
                )
            except (OSError, ValueError) as error:
                results.append(
                    BootstrapResult(
                        origin=origin,
                        peer_node_id=None,
                        accepted_topics=(),
                        error=f"{type(error).__name__}: {error}"[:280],
                    )
                )
        self.last_bootstrap_results = tuple(results)
        return self.last_bootstrap_results

    def send_claim(
        self,
        peer_origin: str,
        signed_claim: SignedClaimV1,
        topic: str,
        *,
        expected_peer_node_id: str | None = None,
        at: datetime | None = None,
    ) -> ClaimExchangeReceipt:
        return post_claim_to_peer(
            peer_origin,
            self.transport.signed_manifest,
            signed_claim,
            topic,
            expected_peer_node_id=expected_peer_node_id,
            at=at,
            allow_private_addresses=self.config.allow_private_bootstrap,
        )

    def build_scan_claim(
        self,
        row: Mapping[str, Any],
        *,
        at: datetime | None = None,
    ) -> SignedClaimV1:
        """Turn one local scanner row into a signed, provider-safe observation."""

        issued_at = normalize_utc(at or datetime.now(UTC), field_name="claim issued_at")
        observed_at = _row_time(row.get("captured_at"), issued_at)
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            raise ValueError("scan row ticker cannot be empty")
        snapshot_id = str(row.get("id") or "").strip()
        if not snapshot_id:
            raise ValueError("scan row id cannot be empty")
        scoring_version = str(row.get("scoring_version") or self.config.software_version).strip()
        if not scoring_version:
            raise ValueError("scan scoring version cannot be empty")

        evidence_receipt = {
            "captured_at": observed_at.isoformat(),
            "scoring_version": scoring_version,
            "snapshot_id": snapshot_id,
            "ticker": ticker,
        }
        evidence_id = content_id(canonical_json_bytes(evidence_receipt))
        evidence = EvidenceReferenceV1(
            evidence_id=evidence_id,
            family="market",
            source="runner-watch.scan",
            observed_at=observed_at,
            locator=f"runner-watch:snapshot:{snapshot_id}"[:512],
        )
        hard_veto = bool(row.get("hard_veto"))
        reason = str(row.get("state_reason") or "Local scanner state is available.").strip()[:280]
        vetoes = (
            (
                RiskVetoV1(
                    code="local.hard_veto",
                    reason=reason,
                    evidence_ids=(evidence_id,),
                ),
            )
            if hard_veto
            else ()
        )
        rug_score = row.get("rug_score")
        claim = RunnerObservationV1(
            issuer_node_id=self.identity.node_id,
            issuer_public_key=self.identity.public_key,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=self.config.claim_ttl_seconds),
            instrument=f"US:{ticker}",
            observed_at=observed_at,
            scanner_version=self.config.software_version,
            schema_version=self.config.claim_schema_versions[0],
            source_versions=(
                SourceVersionV1(
                    family="market",
                    source="runner-watch.scan",
                    version=scoring_version[:64],
                ),
            ),
            setup_score_milli=_score_milli(row.get("setup_score", row.get("score"))),
            rug_score_milli=_score_milli(rug_score) if rug_score is not None else None,
            rug_level=RiskLevel(str(row.get("rug_level") or "UNKNOWN").upper()),
            trade_state=TradeState(str(row.get("trade_state") or "WATCH").upper()),
            state_reason=reason,
            signals=_signals(row.get("signals_json", row.get("signals"))),
            risk_vetoes=vetoes,
            evidence=(evidence,),
        )
        return SignedClaimV1.sign(claim, self.identity.private_key)

    def publish_scan_rows(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        at: datetime | None = None,
    ) -> ClaimPublishSummary:
        """Sign top scanner rows and fan them out to currently negotiated peers."""

        selected = tuple(rows[: self.config.max_claims_per_scan])
        claims: list[SignedClaimV1] = []
        failed = 0
        for row in selected:
            try:
                claims.append(self.build_scan_claim(row, at=at))
            except (TypeError, ValueError):
                failed += 1

        succeeded = 0
        for peer in self.last_bootstrap_results:
            if not peer.connected:
                continue
            topic = next(
                (topic for topic in self.config.topics if topic in peer.accepted_topics),
                None,
            )
            if topic is None:
                continue
            for signed_claim in claims:
                try:
                    self.send_claim(
                        peer.origin,
                        signed_claim,
                        topic,
                        expected_peer_node_id=peer.peer_node_id,
                        at=at,
                    )
                    succeeded += 1
                except (OSError, ValueError):
                    failed += 1
        return ClaimPublishSummary(
            rows_seen=len(rows),
            claims_built=len(claims),
            deliveries_succeeded=succeeded,
            deliveries_failed=failed,
        )

    def close(self) -> None:
        self.peer_store.close()
