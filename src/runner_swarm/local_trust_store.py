from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock

from runner_swarm.key_rotation import (
    LocalKeyRotationRegistry,
    LocalRotationDecision,
    RotationDecisionStatus,
    SignedKeyRotationV1,
)
from runner_swarm.protocol import canonical_json_bytes, content_id
from runner_swarm.reputation import (
    ClaimOutcomeRecord,
    LocalOutcomeLedger,
    OutcomeVerdict,
    PeerReputation,
    ReputationPolicy,
    score_peer_reputation,
)
from runner_swarm.signed_claim import SignedClaimV1

DEFAULT_MAX_OUTCOMES_PER_PEER = 10_000
DEFAULT_MAX_TOTAL_OUTCOMES = 100_000
DEFAULT_MAX_ROTATION_DECISIONS = 10_000


class LocalTrustStoreError(ValueError):
    pass


class LocalTrustStore:
    def __init__(
        self,
        path: str | Path,
        *,
        max_outcomes_per_peer: int = DEFAULT_MAX_OUTCOMES_PER_PEER,
        max_total_outcomes: int = DEFAULT_MAX_TOTAL_OUTCOMES,
        max_rotation_decisions: int = DEFAULT_MAX_ROTATION_DECISIONS,
    ) -> None:
        if max_outcomes_per_peer < 1:
            raise ValueError("max_outcomes_per_peer must be positive")
        if max_total_outcomes < max_outcomes_per_peer:
            raise ValueError("max_total_outcomes cannot be smaller than the per-peer limit")
        if max_rotation_decisions < 1:
            raise ValueError("max_rotation_decisions must be positive")
        self.path = str(path)
        self.max_outcomes_per_peer = max_outcomes_per_peer
        self.max_total_outcomes = max_total_outcomes
        self.max_rotation_decisions = max_rotation_decisions
        self._lock = RLock()
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> LocalTrustStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record_outcome(
        self,
        signed_claim: SignedClaimV1,
        *,
        verdict: OutcomeVerdict,
        measured_at: datetime,
        verified_claim_source_families: tuple[str, ...],
        verification_source_families: tuple[str, ...],
    ) -> ClaimOutcomeRecord:

        record = LocalOutcomeLedger().record(
            signed_claim,
            verdict=verdict,
            measured_at=measured_at,
            verified_claim_source_families=verified_claim_source_families,
            verification_source_families=verification_source_families,
        )
        return self.add_outcome(record)

    def add_outcome(self, record: ClaimOutcomeRecord) -> ClaimOutcomeRecord:

        payload = canonical_json_bytes(record)
        measured_at = record.measured_at.isoformat(timespec="microseconds")
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT payload FROM swarm_local_outcomes WHERE claim_id = ?",
                    (record.claim_id,),
                ).fetchone()
                if existing is not None and bytes(existing[0]) != payload:
                    raise LocalTrustStoreError("A claim already has a different local outcome")
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO swarm_local_outcomes
                        (claim_id, peer_node_id, measured_at, payload)
                    VALUES (?, ?, ?, ?)
                    """,
                    (record.claim_id, record.peer_node_id, measured_at, payload),
                )
                self._prune_outcomes(record.peer_node_id)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return record

    def outcomes_for(self, peer_node_id: str) -> tuple[ClaimOutcomeRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT claim_id, peer_node_id, payload
                FROM swarm_local_outcomes
                WHERE peer_node_id = ?
                ORDER BY measured_at, claim_id
                """,
                (peer_node_id,),
            ).fetchall()
        return self._outcome_records_from_rows(rows)

    def _outcome_records_from_rows(
        self,
        rows: list[tuple[object, object, object]],
    ) -> tuple[ClaimOutcomeRecord, ...]:
        records: list[ClaimOutcomeRecord] = []
        for claim_id, stored_peer_node_id, payload in rows:
            record = ClaimOutcomeRecord.model_validate_json(bytes(payload))
            if record.claim_id != claim_id or record.peer_node_id != stored_peer_node_id:
                raise LocalTrustStoreError("Stored outcome index does not match its payload")
            records.append(record)
        return tuple(records)

    def score(
        self,
        peer_node_id: str,
        policy: ReputationPolicy | None = None,
    ) -> PeerReputation:
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                registry = self._load_rotation_registry()
                current_node_id = registry.resolve(peer_node_id)
                rows = self._connection.execute(
                    """
                    SELECT claim_id, peer_node_id, payload
                    FROM swarm_local_outcomes
                    ORDER BY measured_at, claim_id
                    """
                ).fetchall()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        records = self._outcome_records_from_rows(rows)
        continuity_records = tuple(
            record.model_copy(update={"peer_node_id": current_node_id})
            for record in records
            if registry.resolve(record.peer_node_id) == current_node_id
        )
        return score_peer_reputation(current_node_id, continuity_records, policy)

    def accept_rotation(
        self,
        signed: SignedKeyRotationV1,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                registry = self._load_rotation_registry()
                decision = registry.accept(signed, decided_at=decided_at, reason=reason)
                self._persist_rotation_decision(signed, decision)
                self._connection.commit()
                return decision
            except Exception:
                self._connection.rollback()
                raise

    def reject_rotation(
        self,
        signed: SignedKeyRotationV1,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                registry = self._load_rotation_registry()
                decision = registry.reject(signed, decided_at=decided_at, reason=reason)
                self._persist_rotation_decision(signed, decision)
                self._connection.commit()
                return decision
            except Exception:
                self._connection.rollback()
                raise

    def revoke_rotation(
        self,
        rotation_content_id: str,
        *,
        decided_at: datetime,
        reason: str,
    ) -> LocalRotationDecision:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                registry = self._load_rotation_registry()
                decision = registry.revoke(
                    rotation_content_id,
                    decided_at=decided_at,
                    reason=reason,
                )
                signed = self._load_rotation_artifact(rotation_content_id)
                self._persist_rotation_decision(signed, decision)
                self._connection.commit()
                return decision
            except Exception:
                self._connection.rollback()
                raise

    def rotation_registry(self) -> LocalKeyRotationRegistry:
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                registry = self._load_rotation_registry()
                self._connection.commit()
                return registry
            except Exception:
                self._connection.rollback()
                raise

    def resolve_node_id(self, node_id: str) -> str:
        return self.rotation_registry().resolve(node_id)

    def is_continuation(self, original_node_id: str, presented_node_id: str) -> bool:
        return self.rotation_registry().is_continuation(original_node_id, presented_node_id)

    def continuity_node_ids(self, node_id: str) -> tuple[str, ...]:
        return self.rotation_registry().continuity_node_ids(node_id)

    def _initialize(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS swarm_local_outcomes (
                    claim_id TEXT PRIMARY KEY,
                    peer_node_id TEXT NOT NULL,
                    measured_at TEXT NOT NULL,
                    payload BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS swarm_local_outcomes_peer_time
                    ON swarm_local_outcomes(peer_node_id, measured_at, claim_id);

                CREATE TABLE IF NOT EXISTS swarm_local_rotation_artifacts (
                    content_id TEXT PRIMARY KEY,
                    wire_bytes BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS swarm_local_rotation_decisions (
                    decision_id TEXT PRIMARY KEY,
                    rotation_content_id TEXT NOT NULL,
                    decision_index INTEGER NOT NULL UNIQUE,
                    payload BLOB NOT NULL,
                    FOREIGN KEY(rotation_content_id)
                        REFERENCES swarm_local_rotation_artifacts(content_id)
                );
                CREATE INDEX IF NOT EXISTS swarm_local_rotation_decisions_artifact
                    ON swarm_local_rotation_decisions(rotation_content_id, decision_index);
                """
            )

    def _prune_outcomes(self, peer_node_id: str) -> None:
        self._connection.execute(
            """
            DELETE FROM swarm_local_outcomes
            WHERE claim_id IN (
                SELECT claim_id
                FROM swarm_local_outcomes
                WHERE peer_node_id = ?
                ORDER BY measured_at DESC, claim_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (peer_node_id, self.max_outcomes_per_peer),
        )
        self._connection.execute(
            """
            DELETE FROM swarm_local_outcomes
            WHERE claim_id IN (
                SELECT claim_id
                FROM swarm_local_outcomes
                ORDER BY measured_at DESC, claim_id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self.max_total_outcomes,),
        )

    def _persist_rotation_decision(
        self,
        signed: SignedKeyRotationV1,
        decision: LocalRotationDecision,
    ) -> None:
        wire_bytes = signed.to_wire_bytes()
        decision_payload = canonical_json_bytes(decision)
        decision_id = content_id(decision_payload)
        existing = self._connection.execute(
            """
            SELECT wire_bytes
            FROM swarm_local_rotation_artifacts
            WHERE content_id = ?
            """,
            (signed.content_id,),
        ).fetchone()
        if existing is not None and bytes(existing[0]) != wire_bytes:
            raise LocalTrustStoreError("Stored rotation artifact does not match its content ID")
        self._connection.execute(
            """
            INSERT OR IGNORE INTO swarm_local_rotation_artifacts(content_id, wire_bytes)
            VALUES (?, ?)
            """,
            (signed.content_id, wire_bytes),
        )
        existing_decision = self._connection.execute(
            """
            SELECT payload
            FROM swarm_local_rotation_decisions
            WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        if existing_decision is not None:
            if bytes(existing_decision[0]) != decision_payload:
                raise LocalTrustStoreError("Stored rotation decision hash collision")
            return
        count = self._connection.execute(
            "SELECT COUNT(*) FROM swarm_local_rotation_decisions"
        ).fetchone()[0]
        if count >= self.max_rotation_decisions:
            raise LocalTrustStoreError("Local rotation decision limit has been reached")
        next_index = self._connection.execute(
            "SELECT COALESCE(MAX(decision_index), 0) + 1 FROM swarm_local_rotation_decisions"
        ).fetchone()[0]
        self._connection.execute(
            """
            INSERT INTO swarm_local_rotation_decisions
                (decision_id, rotation_content_id, decision_index, payload)
            VALUES (?, ?, ?, ?)
            """,
            (decision_id, signed.content_id, next_index, decision_payload),
        )

    def _load_rotation_registry(self) -> LocalKeyRotationRegistry:
        artifact_rows = self._connection.execute(
            "SELECT content_id, wire_bytes FROM swarm_local_rotation_artifacts"
        ).fetchall()
        artifacts: dict[str, SignedKeyRotationV1] = {}
        for stored_content_id, wire_bytes in artifact_rows:
            signed = SignedKeyRotationV1.from_wire_bytes(bytes(wire_bytes))
            if signed.content_id != stored_content_id:
                raise LocalTrustStoreError("Stored rotation index does not match its artifact")
            artifacts[str(stored_content_id)] = signed
        decision_rows = self._connection.execute(
            """
            SELECT decision_id, rotation_content_id, payload
            FROM swarm_local_rotation_decisions
            ORDER BY decision_index
            """
        ).fetchall()
        registry = LocalKeyRotationRegistry()
        for decision_id, rotation_content_id, payload in decision_rows:
            signed = artifacts.get(str(rotation_content_id))
            if signed is None:
                raise LocalTrustStoreError("A local rotation decision is missing its artifact")
            expected = LocalRotationDecision.model_validate_json(bytes(payload))
            if content_id(canonical_json_bytes(expected)) != decision_id:
                raise LocalTrustStoreError("Stored rotation decision ID does not match its payload")
            if expected.status == RotationDecisionStatus.ACCEPTED:
                restored = registry.accept(
                    signed,
                    decided_at=expected.decided_at,
                    reason=expected.reason,
                )
            elif expected.status == RotationDecisionStatus.REJECTED:
                restored = registry.reject(
                    signed,
                    decided_at=expected.decided_at,
                    reason=expected.reason,
                )
            else:
                restored = registry.revoke(
                    signed.content_id,
                    decided_at=expected.decided_at,
                    reason=expected.reason,
                )
            if restored != expected:
                raise LocalTrustStoreError("Stored rotation decision is inconsistent")
        return registry

    def _load_rotation_artifact(self, rotation_content_id: str) -> SignedKeyRotationV1:
        row = self._connection.execute(
            """
            SELECT wire_bytes
            FROM swarm_local_rotation_artifacts
            WHERE content_id = ?
            """,
            (rotation_content_id,),
        ).fetchone()
        if row is None:
            raise LocalTrustStoreError("Rotation artifact is not stored locally")
        return SignedKeyRotationV1.from_wire_bytes(bytes(row[0]))
