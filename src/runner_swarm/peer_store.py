"""Bounded, local-only storage and safety policy for untrusted peer claims."""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from runner_swarm.protocol import NODE_ID_PATTERN, normalize_utc
from runner_swarm.signed_claim import CLAIM_ID_PATTERN, RunnerObservationV1, SignedClaimV1

DEFAULT_PEER_STORE_PATH = Path("data/swarm/peer_claims.sqlite3")
_TOPIC_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,95}$")
_CLAIM_ID_PATTERN = re.compile(CLAIM_ID_PATTERN)


class PeerStoreError(RuntimeError):
    """The local peer store could not complete an operation."""


class ClaimState(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    REVOKED = "revoked"


class IngestOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    BANNED = "banned"
    RATE_LIMITED = "rate_limited"


@dataclass(frozen=True, slots=True)
class PeerStoreLimits:
    """Local resource limits. They are not part of the signed wire protocol."""

    claims_per_window: int = 120
    rate_window: timedelta = timedelta(minutes=1)
    max_claims: int = 10_000
    max_audit_events: int = 5_000
    max_control_records: int = 5_000
    max_rate_windows: int = 10_000
    inactive_retention: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        if self.claims_per_window < 1:
            raise ValueError("claims_per_window must be at least 1")
        if self.rate_window <= timedelta(0):
            raise ValueError("rate_window must be positive")
        if self.max_claims < 1:
            raise ValueError("max_claims must be at least 1")
        if self.max_audit_events < 1:
            raise ValueError("max_audit_events must be at least 1")
        if self.max_control_records < 1:
            raise ValueError("max_control_records must be at least 1")
        if self.max_rate_windows < 1:
            raise ValueError("max_rate_windows must be at least 1")
        if self.inactive_retention < timedelta(0):
            raise ValueError("inactive_retention cannot be negative")


@dataclass(frozen=True, slots=True)
class IngestResult:
    outcome: IngestOutcome
    claim_id: str
    state: ClaimState | None

    @property
    def accepted(self) -> bool:
        return self.outcome == IngestOutcome.ACCEPTED


@dataclass(frozen=True, slots=True)
class StoredPeerClaim:
    signed_claim: SignedClaimV1
    topic: str
    state: ClaimState
    received_at: datetime


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: int
    event_at: datetime
    action: str
    outcome: str
    issuer_node_id: str | None
    topic: str | None
    claim_id: str | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class PruneResult:
    claims: int
    audit_events: int
    rate_windows: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS peer_claims (
    claim_id TEXT PRIMARY KEY,
    issuer_node_id TEXT NOT NULL,
    issuer_public_key TEXT NOT NULL,
    topic TEXT NOT NULL,
    kind TEXT NOT NULL,
    instrument TEXT,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    relation_claim_id TEXT,
    state TEXT NOT NULL,
    state_changed_at TEXT NOT NULL,
    wire_bytes BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS peer_claims_current_idx
    ON peer_claims(state, topic, instrument, expires_at);
CREATE INDEX IF NOT EXISTS peer_claims_issuer_idx
    ON peer_claims(issuer_node_id, issued_at);
CREATE INDEX IF NOT EXISTS peer_claims_relation_idx
    ON peer_claims(relation_claim_id);

CREATE TABLE IF NOT EXISTS peer_rate_windows (
    issuer_node_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    window_start TEXT NOT NULL,
    claim_count INTEGER NOT NULL,
    PRIMARY KEY (issuer_node_id, topic, window_start)
);

CREATE TABLE IF NOT EXISTS peer_bans (
    issuer_node_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    banned_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS local_claim_revocations (
    claim_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    revoked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS peer_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_at TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    issuer_node_id TEXT,
    topic TEXT,
    claim_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS peer_audit_events_time_idx
    ON peer_audit_events(event_at, id);
"""


def _utc(value: datetime | None = None) -> datetime:
    return normalize_utc(value or datetime.now(UTC), field_name="peer store timestamp")


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _topic(value: str) -> str:
    normalized = value.strip().lower()
    if not _TOPIC_PATTERN.fullmatch(normalized):
        raise ValueError("topic must be a 1-96 character lowercase swarm topic")
    return normalized


def _reason(value: str) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= 280:
        raise ValueError("reason must contain 1-280 characters")
    return normalized


def _node_id(value: str) -> str:
    if not NODE_ID_PATTERN.fullmatch(value):
        raise ValueError("issuer_node_id must use the rati-node:<sha256> format")
    return value


def _claim_id(value: str) -> str:
    if not _CLAIM_ID_PATTERN.fullmatch(value):
        raise ValueError("claim_id must use the sha256:<64 lowercase hex> format")
    return value


class PeerClaimStore:
    """A dedicated SQLite trust boundary for signed, untrusted peer claims.

    This store deliberately has no connection to runner_web's database layer or
    provider evidence tables. Runtime code can read current peer statements here,
    then apply its own reputation and risk policy before using them.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_PEER_STORE_PATH,
        *,
        limits: PeerStoreLimits | None = None,
    ) -> None:
        self.path = Path(path)
        self.limits = limits or PeerStoreLimits()
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
                connection.executescript(_SCHEMA)
                connection.commit()
            except (OSError, sqlite3.Error) as error:
                raise PeerStoreError(
                    f"Peer store at {self.path} is unavailable: {error}"
                ) from error
            self._connection = connection
        return self._connection

    def ingest_wire(
        self,
        wire_bytes: bytes,
        *,
        topic: str,
        received_at: datetime | None = None,
    ) -> IngestResult:
        """Parse, authenticate, and store one canonical signed wire message."""

        return self.ingest(
            SignedClaimV1.from_wire_bytes(wire_bytes),
            topic=topic,
            received_at=received_at,
        )

    def ingest(
        self,
        signed_claim: SignedClaimV1,
        *,
        topic: str,
        received_at: datetime | None = None,
    ) -> IngestResult:
        """Authenticate and admit one claim under local safety policy."""

        checked_at = _utc(received_at)
        normalized_topic = _topic(topic)
        signed_claim.verify(at=checked_at)
        claim = signed_claim.claim
        issuer = claim.issuer_node_id

        try:
            with self._lock:
                database = self._connect()
                database.execute("BEGIN IMMEDIATE")
                if self._is_banned_locked(database, issuer, checked_at):
                    self._audit_locked(
                        database,
                        at=checked_at,
                        action="ingest",
                        outcome=IngestOutcome.BANNED,
                        issuer=issuer,
                        topic=normalized_topic,
                        claim_id=signed_claim.claim_id,
                    )
                    self._bound_audit_locked(database)
                    database.commit()
                    return IngestResult(IngestOutcome.BANNED, signed_claim.claim_id, None)

                if not self._take_rate_slot_locked(database, issuer, normalized_topic, checked_at):
                    self._audit_locked(
                        database,
                        at=checked_at,
                        action="ingest",
                        outcome=IngestOutcome.RATE_LIMITED,
                        issuer=issuer,
                        topic=normalized_topic,
                        claim_id=signed_claim.claim_id,
                    )
                    self._bound_audit_locked(database)
                    database.commit()
                    return IngestResult(IngestOutcome.RATE_LIMITED, signed_claim.claim_id, None)

                duplicate = database.execute(
                    "SELECT state FROM peer_claims WHERE claim_id = ?",
                    (signed_claim.claim_id,),
                ).fetchone()
                if duplicate is not None:
                    state = ClaimState(str(duplicate["state"]))
                    self._audit_locked(
                        database,
                        at=checked_at,
                        action="ingest",
                        outcome=IngestOutcome.DUPLICATE,
                        issuer=issuer,
                        topic=normalized_topic,
                        claim_id=signed_claim.claim_id,
                    )
                    self._bound_audit_locked(database)
                    database.commit()
                    return IngestResult(IngestOutcome.DUPLICATE, signed_claim.claim_id, state)

                instrument = claim.instrument if isinstance(claim, RunnerObservationV1) else None
                relation = (
                    claim.supersedes_claim_id
                    if isinstance(claim, RunnerObservationV1)
                    else claim.target_claim_id
                )
                now_text = _timestamp(checked_at)
                database.execute(
                    """
                    INSERT INTO peer_claims (
                        claim_id, issuer_node_id, issuer_public_key, topic, kind, instrument,
                        issued_at, expires_at, received_at, relation_claim_id, state,
                        state_changed_at, wire_bytes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        signed_claim.claim_id,
                        issuer,
                        claim.issuer_public_key,
                        normalized_topic,
                        claim.kind,
                        instrument,
                        _timestamp(claim.issued_at),
                        _timestamp(claim.expires_at),
                        now_text,
                        relation,
                        ClaimState.ACTIVE,
                        now_text,
                        signed_claim.to_wire_bytes(),
                    ),
                )
                self._refresh_states_locked(database, checked_at)
                stored = database.execute(
                    "SELECT state FROM peer_claims WHERE claim_id = ?",
                    (signed_claim.claim_id,),
                ).fetchone()
                state = ClaimState(str(stored["state"]))
                self._audit_locked(
                    database,
                    at=checked_at,
                    action="ingest",
                    outcome=IngestOutcome.ACCEPTED,
                    issuer=issuer,
                    topic=normalized_topic,
                    claim_id=signed_claim.claim_id,
                    detail=state,
                )
                self._prune_locked(database, checked_at)
                database.commit()
                return IngestResult(IngestOutcome.ACCEPTED, signed_claim.claim_id, state)
        except sqlite3.Error as error:
            self._rollback()
            raise PeerStoreError(f"Peer claim ingest failed: {error}") from error

    def current_claims(
        self,
        *,
        topic: str | None = None,
        instrument: str | None = None,
        issuer_node_id: str | None = None,
        at: datetime | None = None,
        limit: int = 500,
    ) -> tuple[StoredPeerClaim, ...]:
        """Return current, locally allowed claims; never provider records."""

        if limit < 1 or limit > 5_000:
            raise ValueError("limit must be between 1 and 5000")
        checked_at = _utc(at)
        clauses = ["c.state = ?", "c.kind = 'runner_observation'"]
        values: list[object] = [ClaimState.ACTIVE]
        if topic is not None:
            clauses.append("c.topic = ?")
            values.append(_topic(topic))
        if instrument is not None:
            clauses.append("c.instrument = ?")
            values.append(instrument.strip().upper())
        if issuer_node_id is not None:
            clauses.append("c.issuer_node_id = ?")
            values.append(_node_id(issuer_node_id))
        clauses.append(
            "NOT EXISTS (SELECT 1 FROM peer_bans b WHERE b.issuer_node_id = c.issuer_node_id "
            "AND (b.expires_at IS NULL OR b.expires_at > ?))"
        )
        values.extend((_timestamp(checked_at), limit))

        try:
            with self._lock:
                database = self._connect()
                self._refresh_states_locked(database, checked_at)
                rows = database.execute(
                    "SELECT c.* FROM peer_claims c WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY c.issued_at DESC, c.claim_id LIMIT ?",
                    tuple(values),
                ).fetchall()
                database.commit()
        except sqlite3.Error as error:
            self._rollback()
            raise PeerStoreError(f"Peer claim read failed: {error}") from error
        return tuple(self._stored_claim(row) for row in rows)

    def get_claim(self, claim_id: str, *, at: datetime | None = None) -> StoredPeerClaim | None:
        claim_id = _claim_id(claim_id)
        checked_at = _utc(at)
        try:
            with self._lock:
                database = self._connect()
                self._refresh_states_locked(database, checked_at)
                row = database.execute(
                    "SELECT * FROM peer_claims WHERE claim_id = ?", (claim_id,)
                ).fetchone()
                database.commit()
        except sqlite3.Error as error:
            self._rollback()
            raise PeerStoreError(f"Peer claim read failed: {error}") from error
        return self._stored_claim(row) if row is not None else None

    def ban_peer(
        self,
        issuer_node_id: str,
        *,
        reason: str,
        until: datetime | None = None,
        at: datetime | None = None,
    ) -> None:
        issuer = _node_id(issuer_node_id)
        checked_at = _utc(at)
        expires_at = _utc(until) if until is not None else None
        if expires_at is not None and expires_at <= checked_at:
            raise ValueError("ban expiry must be after the ban time")
        clean_reason = _reason(reason)
        try:
            with self._lock, self._connect() as database:
                self._require_control_capacity_locked(
                    database, "peer_bans", "issuer_node_id", issuer
                )
                database.execute(
                    "INSERT OR REPLACE INTO peer_bans VALUES (?, ?, ?, ?)",
                    (
                        issuer,
                        clean_reason,
                        _timestamp(checked_at),
                        _timestamp(expires_at) if expires_at else None,
                    ),
                )
                self._audit_locked(
                    database,
                    at=checked_at,
                    action="ban_peer",
                    outcome="applied",
                    issuer=issuer,
                    detail=clean_reason,
                )
                self._bound_audit_locked(database)
        except sqlite3.Error as error:
            raise PeerStoreError(f"Peer ban failed: {error}") from error

    def unban_peer(self, issuer_node_id: str, *, at: datetime | None = None) -> bool:
        issuer = _node_id(issuer_node_id)
        checked_at = _utc(at)
        try:
            with self._lock, self._connect() as database:
                removed = database.execute(
                    "DELETE FROM peer_bans WHERE issuer_node_id = ?", (issuer,)
                ).rowcount
                self._audit_locked(
                    database,
                    at=checked_at,
                    action="unban_peer",
                    outcome="removed" if removed else "not_found",
                    issuer=issuer,
                )
                self._bound_audit_locked(database)
        except sqlite3.Error as error:
            raise PeerStoreError(f"Peer unban failed: {error}") from error
        return bool(removed)

    def is_banned(self, issuer_node_id: str, *, at: datetime | None = None) -> bool:
        issuer = _node_id(issuer_node_id)
        checked_at = _utc(at)
        try:
            with self._lock:
                return self._is_banned_locked(self._connect(), issuer, checked_at)
        except sqlite3.Error as error:
            raise PeerStoreError(f"Peer ban lookup failed: {error}") from error

    def revoke_claim(
        self,
        claim_id: str,
        *,
        reason: str,
        at: datetime | None = None,
    ) -> None:
        claim_id = _claim_id(claim_id)
        checked_at = _utc(at)
        clean_reason = _reason(reason)
        try:
            with self._lock, self._connect() as database:
                self._require_control_capacity_locked(
                    database, "local_claim_revocations", "claim_id", claim_id
                )
                database.execute(
                    "INSERT OR REPLACE INTO local_claim_revocations VALUES (?, ?, ?)",
                    (claim_id, clean_reason, _timestamp(checked_at)),
                )
                self._refresh_states_locked(database, checked_at)
                self._audit_locked(
                    database,
                    at=checked_at,
                    action="revoke_claim",
                    outcome="applied",
                    claim_id=claim_id,
                    detail=clean_reason,
                )
                self._bound_audit_locked(database)
        except sqlite3.Error as error:
            raise PeerStoreError(f"Local claim revocation failed: {error}") from error

    def restore_claim(self, claim_id: str, *, at: datetime | None = None) -> bool:
        claim_id = _claim_id(claim_id)
        checked_at = _utc(at)
        try:
            with self._lock, self._connect() as database:
                removed = database.execute(
                    "DELETE FROM local_claim_revocations WHERE claim_id = ?", (claim_id,)
                ).rowcount
                self._refresh_states_locked(database, checked_at)
                self._audit_locked(
                    database,
                    at=checked_at,
                    action="restore_claim",
                    outcome="removed" if removed else "not_found",
                    claim_id=claim_id,
                )
                self._bound_audit_locked(database)
        except sqlite3.Error as error:
            raise PeerStoreError(f"Local claim restore failed: {error}") from error
        return bool(removed)

    def audit_events(self, *, limit: int = 100) -> tuple[AuditEvent, ...]:
        if limit < 1 or limit > self.limits.max_audit_events:
            raise ValueError("audit limit is outside the configured bound")
        try:
            with self._lock:
                rows = (
                    self._connect()
                    .execute("SELECT * FROM peer_audit_events ORDER BY id DESC LIMIT ?", (limit,))
                    .fetchall()
                )
        except sqlite3.Error as error:
            raise PeerStoreError(f"Peer audit read failed: {error}") from error
        return tuple(
            AuditEvent(
                event_id=int(row["id"]),
                event_at=_parse_timestamp(str(row["event_at"])),
                action=str(row["action"]),
                outcome=str(row["outcome"]),
                issuer_node_id=row["issuer_node_id"],
                topic=row["topic"],
                claim_id=row["claim_id"],
                detail=row["detail"],
            )
            for row in rows
        )

    def prune(self, *, at: datetime | None = None) -> PruneResult:
        checked_at = _utc(at)
        try:
            with self._lock:
                database = self._connect()
                database.execute("BEGIN IMMEDIATE")
                result = self._prune_locked(database, checked_at)
                database.commit()
                return result
        except sqlite3.Error as error:
            self._rollback()
            raise PeerStoreError(f"Peer store prune failed: {error}") from error

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def __enter__(self) -> PeerClaimStore:
        self._connect()
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def _take_rate_slot_locked(
        self,
        database: sqlite3.Connection,
        issuer: str,
        topic: str,
        checked_at: datetime,
    ) -> bool:
        window_seconds = self.limits.rate_window.total_seconds()
        window_start = datetime.fromtimestamp(
            (checked_at.timestamp() // window_seconds) * window_seconds,
            tz=UTC,
        )
        window_text = _timestamp(window_start)
        row = database.execute(
            "SELECT claim_count FROM peer_rate_windows "
            "WHERE issuer_node_id = ? AND topic = ? AND window_start = ?",
            (issuer, topic, window_text),
        ).fetchone()
        count = int(row["claim_count"]) if row else 0
        if count >= self.limits.claims_per_window:
            return False
        database.execute(
            """
            INSERT INTO peer_rate_windows VALUES (?, ?, ?, 1)
            ON CONFLICT(issuer_node_id, topic, window_start)
            DO UPDATE SET claim_count = claim_count + 1
            """,
            (issuer, topic, window_text),
        )
        return True

    @staticmethod
    def _is_banned_locked(database: sqlite3.Connection, issuer: str, checked_at: datetime) -> bool:
        row = database.execute(
            "SELECT 1 FROM peer_bans WHERE issuer_node_id = ? "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (issuer, _timestamp(checked_at)),
        ).fetchone()
        return row is not None

    def _refresh_states_locked(self, database: sqlite3.Connection, checked_at: datetime) -> None:
        rows = database.execute(
            "SELECT claim_id, issuer_node_id, kind, instrument, issued_at, expires_at, "
            "relation_claim_id, state FROM peer_claims"
        ).fetchall()
        revoked = {
            str(row["claim_id"])
            for row in database.execute("SELECT claim_id FROM local_claim_revocations")
        }
        now_text = _timestamp(checked_at)

        usable_relations = [row for row in rows if str(row["claim_id"]) not in revoked]
        retractions: dict[str, set[str]] = {}
        superseders: dict[str, list[sqlite3.Row]] = {}
        for row in usable_relations:
            target = row["relation_claim_id"]
            if target is None:
                continue
            if row["kind"] == "retraction":
                if str(row["expires_at"]) > now_text:
                    retractions.setdefault(str(target), set()).add(str(row["issuer_node_id"]))
            elif row["kind"] == "runner_observation":
                superseders.setdefault(str(target), []).append(row)

        for row in rows:
            claim_id = str(row["claim_id"])
            if claim_id in revoked:
                state = ClaimState.REVOKED
            elif str(row["expires_at"]) <= now_text:
                state = ClaimState.EXPIRED
            elif row["kind"] != "runner_observation":
                state = ClaimState.ACTIVE
            elif str(row["issuer_node_id"]) in retractions.get(claim_id, set()):
                state = ClaimState.RETRACTED
            elif any(
                candidate["issuer_node_id"] == row["issuer_node_id"]
                and candidate["instrument"] == row["instrument"]
                and candidate["issued_at"] > row["issued_at"]
                for candidate in superseders.get(claim_id, ())
            ):
                state = ClaimState.SUPERSEDED
            else:
                state = ClaimState.ACTIVE

            if str(row["state"]) != state:
                database.execute(
                    "UPDATE peer_claims SET state = ?, state_changed_at = ? WHERE claim_id = ?",
                    (state, now_text, claim_id),
                )

    def _prune_locked(self, database: sqlite3.Connection, checked_at: datetime) -> PruneResult:
        self._refresh_states_locked(database, checked_at)
        cutoff = _timestamp(checked_at - self.limits.inactive_retention)
        claims = database.execute(
            "DELETE FROM peer_claims WHERE state != ? AND expires_at < ?",
            (ClaimState.ACTIVE, cutoff),
        ).rowcount

        count = int(database.execute("SELECT COUNT(*) FROM peer_claims").fetchone()[0])
        overflow = max(0, count - self.limits.max_claims)
        if overflow:
            claims += database.execute(
                "DELETE FROM peer_claims WHERE claim_id IN ("
                "SELECT claim_id FROM peer_claims "
                "ORDER BY CASE WHEN state = 'active' THEN 1 ELSE 0 END, received_at, claim_id "
                "LIMIT ?)",
                (overflow,),
            ).rowcount
            self._refresh_states_locked(database, checked_at)

        current_window = checked_at - self.limits.rate_window
        rate_windows = database.execute(
            "DELETE FROM peer_rate_windows WHERE window_start < ?",
            (_timestamp(current_window),),
        ).rowcount
        window_count = int(database.execute("SELECT COUNT(*) FROM peer_rate_windows").fetchone()[0])
        window_overflow = max(0, window_count - self.limits.max_rate_windows)
        if window_overflow:
            rate_windows += database.execute(
                "DELETE FROM peer_rate_windows WHERE rowid IN ("
                "SELECT rowid FROM peer_rate_windows ORDER BY window_start, rowid LIMIT ?)",
                (window_overflow,),
            ).rowcount
        database.execute(
            "DELETE FROM peer_bans WHERE expires_at IS NOT NULL AND expires_at < ?",
            (cutoff,),
        )
        audit_events = self._bound_audit_locked(database)
        return PruneResult(
            claims=max(0, claims),
            audit_events=max(0, audit_events),
            rate_windows=max(0, rate_windows),
        )

    def _require_control_capacity_locked(
        self,
        database: sqlite3.Connection,
        table: str,
        key_column: str,
        key: str,
    ) -> None:
        existing = database.execute(
            f"SELECT 1 FROM {table} WHERE {key_column} = ?", (key,)
        ).fetchone()
        if existing is not None:
            return
        count = int(database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count >= self.limits.max_control_records:
            raise PeerStoreError(
                "Local peer-control capacity is full; remove a ban or revocation first"
            )

    def _bound_audit_locked(self, database: sqlite3.Connection) -> int:
        count = int(database.execute("SELECT COUNT(*) FROM peer_audit_events").fetchone()[0])
        overflow = max(0, count - self.limits.max_audit_events)
        if not overflow:
            return 0
        return database.execute(
            "DELETE FROM peer_audit_events WHERE id IN ("
            "SELECT id FROM peer_audit_events ORDER BY id LIMIT ?)",
            (overflow,),
        ).rowcount

    @staticmethod
    def _audit_locked(
        database: sqlite3.Connection,
        *,
        at: datetime,
        action: str,
        outcome: str,
        issuer: str | None = None,
        topic: str | None = None,
        claim_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        database.execute(
            "INSERT INTO peer_audit_events "
            "(event_at, action, outcome, issuer_node_id, topic, claim_id, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_timestamp(at), action, str(outcome), issuer, topic, claim_id, detail),
        )

    @staticmethod
    def _stored_claim(row: sqlite3.Row) -> StoredPeerClaim:
        return StoredPeerClaim(
            signed_claim=SignedClaimV1.from_wire_bytes(bytes(row["wire_bytes"])),
            topic=str(row["topic"]),
            state=ClaimState(str(row["state"])),
            received_at=_parse_timestamp(str(row["received_at"])),
        )

    def _rollback(self) -> None:
        if self._connection is not None and self._connection.in_transaction:
            self._connection.rollback()
