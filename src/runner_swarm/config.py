"""Environment-backed configuration shared by local and cloud swarm runtimes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_TOPIC = "markets/equities/us/runners"
MAX_BOOTSTRAP_URLS = 16
MAX_TOPICS = 64


class SwarmMode(StrEnum):
    """Whether the trader stays isolated or connects to configured peers."""

    SOLO = "solo"
    ATTACHED = "attached"


def _bounded_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _enabled(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _csv(env: Mapping[str, str], name: str, default: str = "") -> tuple[str, ...]:
    values = tuple(item.strip() for item in env.get(name, default).split(",") if item.strip())
    if len(values) != len(set(values)):
        raise ValueError(f"{name} cannot contain duplicates")
    return values


def _bootstrap_urls(env: Mapping[str, str]) -> tuple[str, ...]:
    values = _csv(env, "SWARM_BOOTSTRAP_URLS")
    if len(values) > MAX_BOOTSTRAP_URLS:
        raise ValueError(f"SWARM_BOOTSTRAP_URLS cannot contain more than {MAX_BOOTSTRAP_URLS} URLs")
    for value in values:
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("SWARM_BOOTSTRAP_URLS contains a malformed URL") from exc
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("SWARM_BOOTSTRAP_URLS entries must use https:// and include a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "SWARM_BOOTSTRAP_URLS entries cannot contain credentials, queries, or fragments"
            )
    return values


def _topics(env: Mapping[str, str]) -> tuple[str, ...]:
    values = _csv(env, "SWARM_TOPICS", DEFAULT_TOPIC)
    if not values:
        raise ValueError("SWARM_TOPICS must contain at least one topic")
    if len(values) > MAX_TOPICS:
        raise ValueError(f"SWARM_TOPICS cannot contain more than {MAX_TOPICS} topics")
    for value in values:
        if not 1 <= len(value) <= 96 or value.lower() != value:
            raise ValueError(
                "SWARM_TOPICS entries must be lowercase and contain 1 to 96 characters"
            )
        if any(not (character.isalnum() or character in "._/-") for character in value):
            raise ValueError("SWARM_TOPICS entries contain an unsupported character")
    return tuple(sorted(values))


@dataclass(frozen=True, slots=True)
class SwarmRuntimeConfig:
    """One deployable configuration for solo or attached operation."""

    mode: SwarmMode
    key_path: Path
    peer_store_path: Path
    public_url: str | None
    bootstrap_urls: tuple[str, ...]
    topics: tuple[str, ...]
    software_version: str
    manifest_ttl_seconds: int
    peer_rate_limit_per_minute: int
    bootstrap_interval_seconds: int
    allow_private_bootstrap: bool

    @property
    def attached(self) -> bool:
        return self.mode == SwarmMode.ATTACHED

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SwarmRuntimeConfig:
        source = os.environ if env is None else env
        raw_mode = source.get("SWARM_MODE", SwarmMode.SOLO.value).strip().lower()
        try:
            mode = SwarmMode(raw_mode)
        except ValueError as exc:
            raise ValueError("SWARM_MODE must be solo or attached") from exc

        public_url = source.get("SWARM_PUBLIC_URL", "").strip().rstrip("/") or None
        if public_url is not None:
            parsed = urlsplit(public_url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
                raise ValueError("SWARM_PUBLIC_URL must be an https:// origin without a path")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "SWARM_PUBLIC_URL cannot contain credentials, queries, or fragments"
                )

        software_version = source.get("SWARM_SOFTWARE_VERSION", "0.1.0").strip()
        if not software_version:
            raise ValueError("SWARM_SOFTWARE_VERSION cannot be empty")

        return cls(
            mode=mode,
            key_path=Path(source.get("SWARM_KEY_PATH", "data/swarm/node.key")),
            peer_store_path=Path(
                source.get("SWARM_PEER_STORE_PATH", "data/swarm/peer-claims.db")
            ),
            public_url=public_url,
            bootstrap_urls=_bootstrap_urls(source),
            topics=_topics(source),
            software_version=software_version,
            manifest_ttl_seconds=_bounded_int(
                source,
                "SWARM_MANIFEST_TTL_SECONDS",
                86_400,
                minimum=60,
                maximum=604_800,
            ),
            peer_rate_limit_per_minute=_bounded_int(
                source,
                "SWARM_PEER_RATE_LIMIT_PER_MINUTE",
                60,
                minimum=1,
                maximum=10_000,
            ),
            bootstrap_interval_seconds=_bounded_int(
                source,
                "SWARM_BOOTSTRAP_INTERVAL_SECONDS",
                300,
                minimum=30,
                maximum=86_400,
            ),
            allow_private_bootstrap=_enabled(source, "SWARM_ALLOW_PRIVATE_BOOTSTRAP"),
        )
