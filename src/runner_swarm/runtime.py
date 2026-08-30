"""Runtime assembly shared by solo, attached, and alpha-pack trader nodes."""

from __future__ import annotations

import json
import math
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter

from runner_swarm.alpha_pack import (
    MAX_SIGNED_PACK_BYTES,
    AlphaPackError,
    PeerRole,
    SignedAlphaPack,
)
from runner_swarm.config import SwarmRuntimeConfig
from runner_swarm.identity import load_or_create_node_key, private_key_from_text
from runner_swarm.key_rotation import LocalRotationDecision, SignedKeyRotationV1
from runner_swarm.local_trust_store import LocalTrustStore
from runner_swarm.node_manifest import (
    NodeEndpoint,
    NodeManifest,
    SignedNodeManifest,
    VersionedDeclaration,
    sign_node_manifest,
)
from runner_swarm.peer_store import ClaimState, IngestOutcome, PeerClaimStore, PeerStoreLimits
from runner_swarm.protocol import (
    PROTOCOL_VERSION,
    canonical_json_bytes,
    content_id,
    node_id_from_public_key,
    normalize_utc,
    public_key_text,
)
from runner_swarm.remote_policy import (
    RemoteClaimAssessment,
    RemoteClaimUse,
    assess_remote_claim,
)
from runner_swarm.reputation import (
    ClaimOutcomeRecord,
    OutcomeVerdict,
    PeerReputation,
    ReputationPolicy,
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
    well_known_manifest_url,
)

MAX_RUNTIME_BOOTSTRAPS = 16


@dataclass(frozen=True, slots=True)
class AlphaPackRuntimePolicy:
    """Verified pack state reduced to the policy the runtime can enforce."""

    signed_pack: SignedAlphaPack | None
    effective_config: SwarmRuntimeConfig
    bootstrap_peer_ids: tuple[tuple[str, str], ...] = ()
    approved_peer_node_ids: frozenset[str] = frozenset()


def load_signed_alpha_pack(
    path: str | Path,
    *,
    at: datetime | None = None,
) -> SignedAlphaPack:
    """Read one canonical pack from a bounded regular file and verify it."""

    pack_path = Path(path)
    try:
        before = pack_path.lstat()
    except OSError as error:
        raise AlphaPackError(f"Cannot inspect alpha pack at {pack_path}") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise AlphaPackError("Alpha pack path must be a regular file, not a link")
    if before.st_size > MAX_SIGNED_PACK_BYTES:
        raise AlphaPackError("Signed alpha pack exceeds the safe input size")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(pack_path, flags)
    except OSError as error:
        raise AlphaPackError(f"Cannot open alpha pack at {pack_path}") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise AlphaPackError("Alpha pack path must be a regular file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise AlphaPackError("Alpha pack changed while it was being opened")
        if opened.st_size > MAX_SIGNED_PACK_BYTES:
            raise AlphaPackError("Signed alpha pack exceeds the safe input size")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            wire_bytes = handle.read(MAX_SIGNED_PACK_BYTES + 1)
    except OSError as error:
        raise AlphaPackError(f"Cannot read alpha pack at {pack_path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(wire_bytes) > MAX_SIGNED_PACK_BYTES:
        raise AlphaPackError("Signed alpha pack exceeds the safe input size")

    signed_pack = SignedAlphaPack.from_wire_bytes(wire_bytes)
    signed_pack.verify(at=at)
    return signed_pack


def _canonical_bootstrap_origin(
    value: str,
    *,
    allow_private_addresses: bool,
) -> str | None:
    try:
        manifest_url = well_known_manifest_url(
            value,
            allow_private_addresses=allow_private_addresses,
        )
    except ValueError:
        return None
    parsed = urlsplit(manifest_url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def apply_alpha_pack_policy(
    config: SwarmRuntimeConfig,
    signed_pack: SignedAlphaPack,
) -> AlphaPackRuntimePolicy:
    """Apply a verified pack as a restrictive compatibility and discovery policy."""

    pack = signed_pack.pack
    if PROTOCOL_VERSION not in pack.allowed_claim_versions:
        raise AlphaPackError(f"Alpha pack does not allow claim protocol version {PROTOCOL_VERSION}")

    configured_topics = tuple(config.topics)
    disallowed_topics = sorted(set(configured_topics) - set(pack.topics))
    if disallowed_topics:
        raise AlphaPackError(
            "Configured swarm topics are not allowed by the alpha pack: "
            + ", ".join(disallowed_topics)
        )

    configured_schemas = tuple(getattr(config, "claim_schema_versions", ("runner-v1",)))
    disallowed_schemas = sorted(set(configured_schemas) - set(pack.allowed_schema_versions))
    if disallowed_schemas:
        raise AlphaPackError(
            "Configured claim schemas are not allowed by the alpha pack: "
            + ", ".join(disallowed_schemas)
        )

    allow_private = config.allow_private_bootstrap
    bootstrap_peer_ids: dict[str, str] = {}
    approved_peer_node_ids: set[str] = set()
    for peer in pack.peers:
        if PeerRole.APPROVED in peer.roles:
            approved_peer_node_ids.add(peer.node_id)
        if PeerRole.BOOTSTRAP not in peer.roles:
            continue
        for endpoint in peer.endpoints:
            if urlsplit(endpoint).scheme != "https":
                continue
            origin = _canonical_bootstrap_origin(
                endpoint,
                allow_private_addresses=allow_private,
            )
            if origin is None:
                continue
            previous_peer = bootstrap_peer_ids.get(origin)
            if previous_peer is not None and previous_peer != peer.node_id:
                raise AlphaPackError(
                    "An alpha pack bootstrap origin cannot name more than one peer identity"
                )
            bootstrap_peer_ids[origin] = peer.node_id

    bootstrap_origins: list[str] = []
    for configured_origin in config.bootstrap_urls:
        canonical_origin = _canonical_bootstrap_origin(
            configured_origin,
            allow_private_addresses=allow_private,
        )
        if canonical_origin is None:
            raise AlphaPackError("Configured bootstrap origin fails the safe HTTPS policy")
        if canonical_origin not in bootstrap_origins:
            bootstrap_origins.append(canonical_origin)
    for pack_origin in sorted(bootstrap_peer_ids):
        if pack_origin not in bootstrap_origins:
            bootstrap_origins.append(pack_origin)
    if len(bootstrap_origins) > MAX_RUNTIME_BOOTSTRAPS:
        raise AlphaPackError(
            f"Alpha pack expands runtime bootstrap origins beyond {MAX_RUNTIME_BOOTSTRAPS}"
        )

    effective_config = replace(
        config,
        bootstrap_urls=tuple(bootstrap_origins),
        topics=configured_topics,
    )
    return AlphaPackRuntimePolicy(
        signed_pack=signed_pack,
        effective_config=effective_config,
        bootstrap_peer_ids=tuple(sorted(bootstrap_peer_ids.items())),
        approved_peer_node_ids=frozenset(approved_peer_node_ids),
    )


def prepare_alpha_pack_runtime(
    config: SwarmRuntimeConfig,
    *,
    at: datetime | None = None,
) -> AlphaPackRuntimePolicy:
    """Load and apply the optional signed pack selected by local configuration."""

    alpha_pack_path = getattr(config, "alpha_pack_path", None)
    if alpha_pack_path is None:
        return AlphaPackRuntimePolicy(signed_pack=None, effective_config=config)
    signed_pack = load_signed_alpha_pack(alpha_pack_path, at=at)
    return apply_alpha_pack_policy(config, signed_pack)


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
        local_trust_store: LocalTrustStore,
        *,
        alpha_pack_policy: AlphaPackRuntimePolicy | None = None,
    ) -> None:
        if not config.attached:
            raise ValueError("AttachedSwarmRuntime requires SWARM_MODE=attached")
        self.config = config
        self.identity = identity
        self.peer_store = peer_store
        self.local_trust_store = local_trust_store
        self.alpha_pack_policy = alpha_pack_policy or AlphaPackRuntimePolicy(
            signed_pack=None,
            effective_config=config,
        )
        self.signed_alpha_pack = self.alpha_pack_policy.signed_pack
        self._bootstrap_peer_ids = dict(self.alpha_pack_policy.bootstrap_peer_ids)
        self._alpha_pack_revalidation_error: str | None = None
        self.router: APIRouter = create_swarm_router(
            signed_manifest,
            receive_claim=self.receive_peer_claim,
            accepted_claim_schema_versions=frozenset(
                getattr(config, "claim_schema_versions", ("runner-v1",))
            ),
        )
        self.transport: SwarmTransport = self.router.swarm_transport  # type: ignore[attr-defined]
        self.last_bootstrap_results: tuple[BootstrapResult, ...] = ()

    @classmethod
    def open(
        cls,
        config: SwarmRuntimeConfig,
        *,
        at: datetime | None = None,
    ) -> AttachedSwarmRuntime:
        alpha_pack_policy = prepare_alpha_pack_runtime(config, at=at)
        effective_config = alpha_pack_policy.effective_config
        identity = SwarmNodeIdentity.load(effective_config)
        signed_manifest = build_signed_runtime_manifest(effective_config, identity, at=at)
        peer_store = PeerClaimStore(
            effective_config.peer_store_path,
            limits=PeerStoreLimits(
                claims_per_window=effective_config.peer_rate_limit_per_minute,
            ),
        )
        local_trust_path = getattr(
            effective_config,
            "local_trust_store_path",
            effective_config.peer_store_path.with_name("local-trust.db"),
        )
        try:
            local_trust_store = LocalTrustStore(local_trust_path)
        except Exception:
            peer_store.close()
            raise
        try:
            return cls(
                effective_config,
                identity,
                signed_manifest,
                peer_store,
                local_trust_store,
                alpha_pack_policy=alpha_pack_policy,
            )
        except Exception:
            local_trust_store.close()
            peer_store.close()
            raise

    def receive_peer_claim(self, received: ReceivedPeerClaim) -> bool:
        """Persist a verified claim as peer context, never as provider evidence."""

        self._revalidate_alpha_pack(at=received.received_at)
        if self._peer_is_banned(
            received.signed_claim.claim.issuer_node_id,
            at=received.received_at,
        ):
            raise PeerClaimRejected("Peer is locally banned", status_code=403)
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
        self._revalidate_alpha_pack(at=at)
        signed_manifest = build_signed_runtime_manifest(
            self.config,
            self.identity,
            at=at,
        )
        self.transport.replace_local_manifest(signed_manifest)
        return signed_manifest

    def record_peer_outcome(
        self,
        signed_claim: SignedClaimV1,
        *,
        verdict: OutcomeVerdict,
        measured_at: datetime,
        verified_claim_source_families: tuple[str, ...],
        verification_source_families: tuple[str, ...],
    ) -> ClaimOutcomeRecord:
        """Persist an outcome measured locally for one verified remote observation."""

        return self.local_trust_store.record_outcome(
            signed_claim,
            verdict=verdict,
            measured_at=measured_at,
            verified_claim_source_families=verified_claim_source_families,
            verification_source_families=verification_source_families,
        )

    def peer_reputation(self, peer_node_id: str) -> PeerReputation:
        """Score a peer only from this node's durable local outcome history."""

        return self.local_trust_store.score(peer_node_id, self._reputation_policy())

    def assess_peer_claim(
        self,
        signed_claim: SignedClaimV1,
        *,
        local_risk_allows: bool,
        verified_evidence_ids: tuple[str, ...],
        at: datetime | None = None,
    ) -> RemoteClaimAssessment:
        """Assess remote evidence as context only; it can never execute a trade."""

        self._revalidate_alpha_pack(at=at)
        pack = self.signed_alpha_pack.pack if self.signed_alpha_pack is not None else None
        reputation = self.peer_reputation(signed_claim.claim.issuer_node_id)
        assessment = assess_remote_claim(
            signed_claim,
            reputation,
            trust_policy=pack.trust_policy if pack is not None else None,
            evidence_requirements=pack.evidence_requirements if pack is not None else None,
            local_risk_allows=local_risk_allows,
            verified_evidence_ids=verified_evidence_ids,
            at=at,
        )

        extra_reasons: list[str] = []
        claim = signed_claim.claim
        stored_claim = self.peer_store.get_claim(signed_claim.claim_id, at=at)
        if (
            stored_claim is None
            or stored_claim.state != ClaimState.ACTIVE
            or stored_claim.signed_claim != signed_claim
        ):
            extra_reasons.append("claim_not_active_in_peer_store")
        if self._peer_is_banned(claim.issuer_node_id, at=at):
            extra_reasons.append("peer_is_locally_banned")
        if isinstance(claim, RunnerObservationV1) and claim.schema_version not in getattr(
            self.config,
            "claim_schema_versions",
            ("runner-v1",),
        ):
            extra_reasons.append("claim_schema_not_allowed")
        if pack is not None:
            approved_current_ids = {
                self.local_trust_store.resolve_node_id(peer_node_id)
                for peer_node_id in self.alpha_pack_policy.approved_peer_node_ids
            }
            if claim.issuer_node_id not in approved_current_ids:
                extra_reasons.append("peer_not_approved_by_alpha_pack")
        if not extra_reasons:
            return assessment
        return assessment.model_copy(
            update={
                "use": RemoteClaimUse.REJECTED,
                "context_weight_ppm": 0,
                "reasons": tuple(dict.fromkeys((*assessment.reasons, *extra_reasons))),
            }
        )

    def accept_peer_key_rotation(
        self,
        signed_rotation: SignedKeyRotationV1,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        return self.local_trust_store.accept_rotation(
            signed_rotation,
            decided_at=decided_at,
            reason=reason,
        )

    def reject_peer_key_rotation(
        self,
        signed_rotation: SignedKeyRotationV1,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        return self.local_trust_store.reject_rotation(
            signed_rotation,
            decided_at=decided_at,
            reason=reason,
        )

    def revoke_peer_key_rotation(
        self,
        rotation_content_id: str,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        return self.local_trust_store.revoke_rotation(
            rotation_content_id,
            decided_at=decided_at,
            reason=reason,
        )

    def resolve_peer_node_id(self, node_id: str) -> str:
        return self.local_trust_store.resolve_node_id(node_id)

    def _peer_is_banned(self, node_id: str, *, at: datetime | None) -> bool:
        """Apply a local ban to every identity in an accepted rotation chain."""

        return any(
            self.peer_store.is_banned(candidate, at=at)
            for candidate in self.local_trust_store.continuity_node_ids(node_id)
        )

    def _reputation_policy(self) -> ReputationPolicy | None:
        if self.signed_alpha_pack is None:
            return None
        pack = self.signed_alpha_pack.pack
        trust = pack.trust_policy
        evidence = pack.evidence_requirements
        return ReputationPolicy(
            minimum_scored_outcomes=max(1, trust.minimum_scored_outcomes),
            minimum_reputation_ppm=trust.minimum_reputation_ppm,
            maximum_influence_ppm=trust.maximum_peer_claim_weight_ppm,
            target_independent_source_families=max(
                1,
                evidence.minimum_independent_source_families,
            ),
        )

    def _revalidate_alpha_pack(
        self,
        *,
        at: datetime | None,
        reload_file: bool = False,
    ) -> None:
        if self.signed_alpha_pack is None:
            return
        if self._alpha_pack_revalidation_error is not None and not reload_file:
            raise AlphaPackError(self._alpha_pack_revalidation_error)
        try:
            self.signed_alpha_pack.verify(at=at)
            if not reload_file:
                return
            alpha_pack_path = getattr(self.config, "alpha_pack_path", None)
            if alpha_pack_path is None:
                raise AlphaPackError("Attached alpha pack path is no longer configured")
            refreshed = load_signed_alpha_pack(alpha_pack_path, at=at)
            if refreshed.content_id != self.signed_alpha_pack.content_id:
                raise AlphaPackError(
                    "Alpha pack changed; restart the runtime to apply the new policy"
                )
            self._alpha_pack_revalidation_error = None
        except (OSError, ValueError) as error:
            if reload_file:
                self.last_bootstrap_results = ()
                self._alpha_pack_revalidation_error = (
                    "Alpha pack runtime is disabled after revalidation failed: "
                    f"{type(error).__name__}: {error}"
                )[:320]
            raise

    def refresh_bootstraps(self, *, at: datetime | None = None) -> tuple[BootstrapResult, ...]:
        """Discover and negotiate every configured seed without trusting membership."""

        self._revalidate_alpha_pack(at=at, reload_file=True)
        results: list[BootstrapResult] = []
        for origin in self.config.bootstrap_urls:
            try:
                peer_manifest = fetch_signed_manifest(
                    origin,
                    at=at,
                    allow_private_addresses=self.config.allow_private_bootstrap,
                )
                if self._peer_is_banned(peer_manifest.manifest.node_id, at=at):
                    raise ValueError("Bootstrap peer is locally banned")
                original_pack_node_id = self._bootstrap_peer_ids.get(origin)
                expected_pack_node_id = (
                    self.local_trust_store.resolve_node_id(original_pack_node_id)
                    if original_pack_node_id is not None
                    else None
                )
                if (
                    expected_pack_node_id is not None
                    and peer_manifest.manifest.node_id != expected_pack_node_id
                ):
                    raise ValueError("Bootstrap manifest does not match the alpha pack peer")
                response = negotiate_with_peer(
                    origin,
                    self.transport.signed_manifest,
                    self.config.topics,
                    expected_peer_node_id=(expected_pack_node_id or peer_manifest.manifest.node_id),
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
        self._revalidate_alpha_pack(at=at)
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
        try:
            self.local_trust_store.close()
        finally:
            self.peer_store.close()
