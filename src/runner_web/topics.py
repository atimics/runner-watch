from __future__ import annotations

import fnmatch
import json
import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from runner_web.db import connection

TopicStatus = Literal["fresh", "stale", "expired", "error"]


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Topic timestamps must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TopicPolicy:
    ttl_seconds: float
    minimum_refresh_seconds: float = 0
    maximum_stale_seconds: float = 900
    keep_last_good: bool = True

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0 or self.minimum_refresh_seconds < 0:
            raise ValueError("Topic refresh times cannot be negative")
        if self.maximum_stale_seconds < self.ttl_seconds:
            raise ValueError("maximum_stale_seconds must be at least the TTL")


@dataclass(frozen=True, slots=True)
class TopicUpdate:
    data: Any
    source: str
    as_of: datetime
    collected_at: datetime = field(default_factory=_now)
    delayed: bool | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _aware(self.as_of))
        object.__setattr__(self, "collected_at", _aware(self.collected_at))
        if not self.source.strip():
            raise ValueError("A topic update must name its source")


@dataclass(frozen=True, slots=True)
class TopicValue:
    data: Any
    source: str
    as_of: datetime
    collected_at: datetime
    delayed: bool | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class TopicSnapshot:
    topic: str
    data: Any
    source: str
    as_of: datetime
    collected_at: datetime
    status: TopicStatus
    age_seconds: float
    cache_age_seconds: float
    delayed: bool | None = None
    warnings: tuple[str, ...] = ()
    error: str | None = None

    def metadata(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "collected_at": self.collected_at.isoformat(),
            "status": self.status,
            "age_seconds": round(self.age_seconds, 3),
            "cache_age_seconds": round(self.cache_age_seconds, 3),
            "delayed": self.delayed,
            "warnings": list(self.warnings),
            "error": self.error,
        }


class TopicStore(Protocol):
    def load(self, topic: str) -> TopicValue | None: ...

    def save(self, topic: str, value: TopicValue) -> None: ...


class SQLiteTopicStore:
    """Durable last-known-good values for restarts and worker handoffs."""

    def load(self, topic: str) -> TopicValue | None:
        with connection() as database:
            row = database.execute(
                """
                SELECT source,as_of,collected_at,delayed,payload_json,warnings_json,error
                FROM topic_snapshots WHERE topic=?
                """,
                (topic,),
            ).fetchone()
        if row is None:
            return None
        return TopicValue(
            data=json.loads(row["payload_json"]),
            source=str(row["source"]),
            as_of=_aware(datetime.fromisoformat(row["as_of"])),
            collected_at=_aware(datetime.fromisoformat(row["collected_at"])),
            delayed=None if row["delayed"] is None else bool(row["delayed"]),
            warnings=tuple(json.loads(row["warnings_json"])),
            error=row["error"],
        )

    def save(self, topic: str, value: TopicValue) -> None:
        timestamp = _now().isoformat()
        with connection() as database:
            database.execute(
                """
                INSERT INTO topic_snapshots(
                    topic,source,as_of,collected_at,delayed,payload_json,warnings_json,
                    error,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(topic) DO UPDATE SET
                    source=excluded.source,as_of=excluded.as_of,
                    collected_at=excluded.collected_at,delayed=excluded.delayed,
                    payload_json=excluded.payload_json,warnings_json=excluded.warnings_json,
                    error=excluded.error,updated_at=excluded.updated_at
                """,
                (
                    topic,
                    value.source,
                    value.as_of.isoformat(),
                    value.collected_at.isoformat(),
                    None if value.delayed is None else int(value.delayed),
                    json.dumps(value.data, sort_keys=True, separators=(",", ":"), default=str),
                    json.dumps(value.warnings),
                    value.error,
                    timestamp,
                ),
            )


@dataclass(slots=True)
class _Entry:
    value: TopicValue | None = None
    refreshing: bool = False
    last_attempt_at: datetime | None = None
    access_order: int = 0


class TopicHub:
    """Shares refreshes and keeps stale data visible when a source fails."""

    def __init__(
        self,
        *,
        store: TopicStore | None = None,
        clock: Callable[[], datetime] = _now,
        wait_timeout_seconds: float = 30,
        maximum_entries: int = 1000,
    ) -> None:
        if maximum_entries < 1:
            raise ValueError("maximum_entries must be at least one")
        self._store = store
        self._clock = clock
        self._wait_timeout_seconds = wait_timeout_seconds
        self._maximum_entries = maximum_entries
        self._access_order = 0
        self._condition = threading.Condition(threading.RLock())
        self._entries: dict[str, _Entry] = {}
        self._loaded: set[str] = set()
        self._subscribers: list[tuple[str, Callable[[TopicSnapshot], None]]] = []

    def _entry(self, topic: str) -> _Entry:
        entry = self._entries.get(topic)
        if entry is None:
            entry = self._entries.setdefault(topic, _Entry())
        self._access_order += 1
        entry.access_order = self._access_order
        if topic not in self._loaded:
            self._loaded.add(topic)
            if self._store is not None:
                try:
                    entry.value = self._store.load(topic)
                except Exception as exc:
                    logging.getLogger(__name__).warning("Could not load topic %s: %s", topic, exc)
        return entry

    def _evict(self, exclude: set[str]) -> None:
        while len(self._entries) > self._maximum_entries:
            candidates = [
                (name, candidate)
                for name, candidate in self._entries.items()
                if name not in exclude and not candidate.refreshing
            ]
            if not candidates:
                return
            oldest, _ = min(candidates, key=lambda item: item[1].access_order)
            self._entries.pop(oldest, None)
            self._loaded.discard(oldest)

    @staticmethod
    def _elapsed(later: datetime, earlier: datetime) -> float:
        return max(0.0, (later - earlier).total_seconds())

    def _snapshot(
        self,
        topic: str,
        value: TopicValue | None,
        policy: TopicPolicy,
        current: datetime,
    ) -> TopicSnapshot:
        if value is None:
            return TopicSnapshot(
                topic=topic,
                data=None,
                source="unavailable",
                as_of=current,
                collected_at=current,
                status="error",
                age_seconds=0,
                cache_age_seconds=0,
                error="No value has been collected",
            )
        age = self._elapsed(current, value.as_of)
        cache_age = self._elapsed(current, value.collected_at)
        if value.data is None:
            status: TopicStatus = "error"
        elif cache_age > policy.maximum_stale_seconds:
            status = "expired"
        elif value.error or cache_age > policy.ttl_seconds:
            status = "stale"
        else:
            status = "fresh"
        return TopicSnapshot(
            topic=topic,
            data=value.data,
            source=value.source,
            as_of=value.as_of,
            collected_at=value.collected_at,
            status=status,
            age_seconds=age,
            cache_age_seconds=cache_age,
            delayed=value.delayed,
            warnings=value.warnings,
            error=value.error,
        )

    def _save(self, topic: str, value: TopicValue) -> None:
        if self._store is None:
            return
        try:
            self._store.save(topic, value)
        except Exception as exc:
            logging.getLogger(__name__).warning("Could not save topic %s: %s", topic, exc)

    def _failure_value(
        self,
        previous: TopicValue | None,
        error: str,
        policy: TopicPolicy,
        current: datetime,
    ) -> TopicValue:
        if (
            previous is not None
            and previous.data is not None
            and policy.keep_last_good
            and self._elapsed(current, previous.collected_at) <= policy.maximum_stale_seconds
        ):
            return replace(previous, error=error[:1000])
        return TopicValue(
            data=None,
            source=previous.source if previous else "unavailable",
            as_of=previous.as_of if previous else current,
            collected_at=previous.collected_at if previous else current,
            delayed=previous.delayed if previous else None,
            warnings=previous.warnings if previous else (),
            error=error[:1000],
        )

    def get_many(
        self,
        topics: Sequence[str],
        *,
        policy: TopicPolicy,
        producer: Callable[[tuple[str, ...]], Mapping[str, TopicUpdate]],
    ) -> dict[str, TopicSnapshot]:
        requested = tuple(dict.fromkeys(topic.strip() for topic in topics if topic.strip()))
        if not requested:
            return {}
        current = _aware(self._clock())
        claimed: list[str] = []
        waiting: list[str] = []
        with self._condition:
            for topic in requested:
                entry = self._entry(topic)
                snapshot = self._snapshot(topic, entry.value, policy, current)
                needs_refresh = snapshot.status != "fresh"
                if not needs_refresh:
                    continue
                if entry.refreshing:
                    waiting.append(topic)
                    continue
                retry_allowed = (
                    entry.last_attempt_at is None
                    or self._elapsed(current, entry.last_attempt_at)
                    >= policy.minimum_refresh_seconds
                )
                if not retry_allowed:
                    continue
                entry.refreshing = True
                entry.last_attempt_at = current
                claimed.append(topic)

        notifications: list[TopicSnapshot] = []
        if claimed:
            try:
                updates = producer(tuple(claimed))
                producer_error: str | None = None
            except Exception as exc:
                updates = {}
                producer_error = str(exc) or exc.__class__.__name__
            finished = _aware(self._clock())
            saved: list[tuple[str, TopicValue]] = []
            with self._condition:
                for topic in claimed:
                    entry = self._entry(topic)
                    update = updates.get(topic)
                    if not isinstance(update, TopicUpdate):
                        message = producer_error or "Producer returned no value for this topic"
                        entry.value = self._failure_value(entry.value, message, policy, finished)
                    else:
                        entry.value = TopicValue(
                            data=update.data,
                            source=update.source,
                            as_of=update.as_of,
                            collected_at=update.collected_at,
                            delayed=update.delayed,
                            warnings=update.warnings,
                        )
                    entry.refreshing = False
                    saved.append((topic, entry.value))
                    notifications.append(self._snapshot(topic, entry.value, policy, finished))
                self._condition.notify_all()
            for topic, value in saved:
                self._save(topic, value)

        if waiting:
            with self._condition:
                self._condition.wait_for(
                    lambda: all(not self._entry(topic).refreshing for topic in waiting),
                    timeout=self._wait_timeout_seconds,
                )

        for snapshot in notifications:
            self._notify(snapshot)
        finished = _aware(self._clock())
        with self._condition:
            result = {
                topic: self._snapshot(topic, self._entry(topic).value, policy, finished)
                for topic in requested
            }
            self._evict(set(requested))
            return result

    def get_or_refresh(
        self,
        topic: str,
        *,
        policy: TopicPolicy,
        producer: Callable[[], TopicUpdate],
    ) -> TopicSnapshot:
        return self.get_many(
            [topic],
            policy=policy,
            producer=lambda claimed: {claimed[0]: producer()},
        )[topic]

    def peek(self, topic: str, *, policy: TopicPolicy) -> TopicSnapshot:
        current = _aware(self._clock())
        with self._condition:
            snapshot = self._snapshot(topic, self._entry(topic).value, policy, current)
            self._evict({topic})
            return snapshot

    def subscribe(self, pattern: str, callback: Callable[[TopicSnapshot], None]) -> None:
        with self._condition:
            self._subscribers.append((pattern, callback))

    def _notify(self, snapshot: TopicSnapshot) -> None:
        with self._condition:
            callbacks = [
                callback
                for pattern, callback in self._subscribers
                if fnmatch.fnmatchcase(snapshot.topic, pattern)
            ]
        for callback in callbacks:
            try:
                callback(snapshot)
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Topic subscriber failed for %s: %s", snapshot.topic, exc
                )
