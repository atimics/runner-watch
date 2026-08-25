from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

FetchStatus = Literal["success", "partial", "error"]


@dataclass(slots=True)
class SourceFetch:
    """One completed attempt to read data from an outside source."""

    source: str
    feed: str
    locator: str
    status: FetchStatus
    started_at: datetime
    finished_at: datetime
    payload: Any = None
    content_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @classmethod
    def success(
        cls,
        *,
        source: str,
        feed: str,
        locator: str,
        started_at: datetime,
        payload: Any,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
        partial: bool = False,
    ) -> SourceFetch:
        return cls(
            source=source,
            feed=feed,
            locator=locator,
            status="partial" if partial else "success",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            payload=payload,
            content_type=content_type,
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        *,
        source: str,
        feed: str,
        locator: str,
        started_at: datetime,
        error: Exception | str,
        metadata: dict[str, Any] | None = None,
    ) -> SourceFetch:
        return cls(
            source=source,
            feed=feed,
            locator=locator,
            status="error",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            metadata=metadata or {},
            error=str(error)[:1000],
        )


class SourceFetchRecorder(Protocol):
    def __call__(self, fetch: SourceFetch) -> None: ...
