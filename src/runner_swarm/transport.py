"""Safe HTTPS discovery and bounded HTTP exchange for RATi swarm peers."""

from __future__ import annotations

import http.client
import inspect
import ipaddress
import re
import socket
import ssl
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import Field, field_validator

from runner_swarm.node_manifest import (
    MAX_SIGNED_MANIFEST_BYTES,
    NodeManifest,
    SignedNodeManifest,
    VersionedDeclaration,
    verify_signed_node_manifest,
)
from runner_swarm.protocol import (
    NODE_ID_PATTERN,
    PROTOCOL_VERSION,
    SwarmModel,
    canonical_json_bytes,
)
from runner_swarm.signed_claim import (
    CLAIM_ID_PATTERN,
    MAX_WIRE_BYTES,
    RunnerObservationV1,
    SignedClaimV1,
)

WELL_KNOWN_PATH = "/.well-known/rati-swarm.json"
NEGOTIATION_PATH = "/swarm/v1/negotiate"
CLAIM_EXCHANGE_PATH = "/swarm/v1/claims"

MANIFEST_MEDIA_TYPE = "application/rati-swarm+json"
JSON_MEDIA_TYPES = frozenset({"application/json", MANIFEST_MEDIA_TYPE})
MAX_NEGOTIATION_BYTES = 48 * 1024
MAX_CLAIM_EXCHANGE_BYTES = MAX_SIGNED_MANIFEST_BYTES + MAX_WIRE_BYTES + 4 * 1024
DEFAULT_FETCH_TIMEOUT_SECONDS = 5.0
MAX_FETCH_TIMEOUT_SECONDS = 10.0
MAX_RESOLVED_ADDRESSES = 8
DEFAULT_INBOX_SIZE = 1_024

_TOPIC_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._/-][a-z0-9]+)*$")
_BLOCKED_HOST_SUFFIXES = (".home", ".internal", ".lan", ".local", ".localhost")


class DiscoveryError(ValueError):
    """A peer discovery URL or response failed the safe-fetch policy."""


class PeerExchangeError(ValueError):
    """A peer message failed identity, compatibility, or signature checks."""


class PeerClaimRejected(PeerExchangeError):
    """Local peer policy rejected an otherwise transport-valid claim."""

    def __init__(self, message: str, *, status_code: int) -> None:
        if status_code not in {403, 429}:
            raise ValueError("Peer claim rejection status must be 403 or 429")
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(UTC)


def _normalize_topic(value: str) -> str:
    if not 1 <= len(value) <= 96 or not _TOPIC_PATTERN.fullmatch(value):
        raise ValueError("topic must be a lowercase namespaced protocol topic")
    return value


class PeerNegotiationRequest(SwarmModel):
    """A signed peer manifest plus the topics it wants for this connection."""

    peer_manifest: SignedNodeManifest
    requested_topics: Annotated[tuple[str, ...], Field(min_length=1, max_length=64)]

    @field_validator("requested_topics")
    @classmethod
    def validate_requested_topics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        topics = tuple(sorted(_normalize_topic(topic) for topic in value))
        if len(topics) != len(set(topics)):
            raise ValueError("requested_topics cannot contain duplicates")
        return topics


class PeerNegotiationResponse(SwarmModel):
    """The exact intersection accepted by the receiving node."""

    protocol_version: Literal["1"] = PROTOCOL_VERSION
    local_node_id: Annotated[str, Field(pattern=NODE_ID_PATTERN.pattern)]
    peer_node_id: Annotated[str, Field(pattern=NODE_ID_PATTERN.pattern)]
    accepted_topics: Annotated[tuple[str, ...], Field(max_length=64)]
    rejected_topics: Annotated[tuple[str, ...], Field(max_length=64)]
    compatible_claim_schemas: Annotated[tuple[VersionedDeclaration, ...], Field(max_length=32)]
    max_claim_exchange_bytes: Annotated[int, Field(ge=1, le=MAX_CLAIM_EXCHANGE_BYTES)] = (
        MAX_CLAIM_EXCHANGE_BYTES
    )
    require_local_risk_gate: Literal[True] = True

    @field_validator("accepted_topics", "rejected_topics")
    @classmethod
    def validate_topics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        topics = tuple(sorted(_normalize_topic(topic) for topic in value))
        if len(topics) != len(set(topics)):
            raise ValueError("Negotiated topic lists cannot contain duplicates")
        return topics


class ClaimExchangeRequest(SwarmModel):
    """A signed peer identity and signed claim routed under one negotiated topic."""

    topic: str
    peer_manifest: SignedNodeManifest
    signed_claim: SignedClaimV1

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        return _normalize_topic(value)


class ClaimExchangeReceipt(SwarmModel):
    accepted: Literal[True] = True
    duplicate: bool
    claim_id: Annotated[str, Field(pattern=CLAIM_ID_PATTERN)]
    local_node_id: Annotated[str, Field(pattern=NODE_ID_PATTERN.pattern)]
    peer_node_id: Annotated[str, Field(pattern=NODE_ID_PATTERN.pattern)]
    topic: str
    stored_as_untrusted_peer_claim: Literal[True] = True
    require_local_risk_gate: Literal[True] = True

    @field_validator("topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        return _normalize_topic(value)


@dataclass(frozen=True, slots=True)
class ReceivedPeerClaim:
    """Verified transport input that must remain outside trusted provider data."""

    topic: str
    peer_manifest: NodeManifest
    signed_claim: SignedClaimV1
    received_at: datetime


class PeerClaimInbox:
    """A small replay filter and volatile inbox for untrusted peer claims."""

    def __init__(self, max_claims: int = DEFAULT_INBOX_SIZE) -> None:
        if not 1 <= max_claims <= 100_000:
            raise ValueError("max_claims must be between 1 and 100000")
        self._claims: deque[ReceivedPeerClaim] = deque()
        self._claim_ids: set[str] = set()
        self._max_claims = max_claims
        self._lock = threading.Lock()

    def receive(self, received: ReceivedPeerClaim) -> bool:
        """Store once, evict oldest when full, and return whether it was new."""

        claim_id = received.signed_claim.claim_id
        with self._lock:
            if claim_id in self._claim_ids:
                return False
            if len(self._claims) >= self._max_claims:
                evicted = self._claims.popleft()
                self._claim_ids.discard(evicted.signed_claim.claim_id)
            self._claims.append(received)
            self._claim_ids.add(claim_id)
            return True

    def snapshot(self) -> tuple[ReceivedPeerClaim, ...]:
        with self._lock:
            return tuple(self._claims)


ClaimReceiver = Callable[[ReceivedPeerClaim], bool | None | Awaitable[bool | None]]


def _declaration_major(version: str) -> int:
    return int(version.split(".", 1)[0])


def _compatible_declarations(
    local: Sequence[VersionedDeclaration],
    peer: Sequence[VersionedDeclaration],
    *,
    name: str | None = None,
) -> tuple[VersionedDeclaration, ...]:
    compatible: dict[tuple[str, str], VersionedDeclaration] = {}
    for local_item in local:
        if name is not None and local_item.name != name:
            continue
        for peer_item in peer:
            if peer_item.name == local_item.name and _declaration_major(
                peer_item.version
            ) == _declaration_major(local_item.version):
                # Report the local implementation. A shared major version means the
                # peers can negotiate minor details without pretending they are equal.
                compatible[(local_item.name, local_item.version)] = local_item
    return tuple(compatible[key] for key in sorted(compatible))


def _has_capability(manifest: NodeManifest, name: str) -> bool:
    return any(
        item.name == name and _declaration_major(item.version) == 1
        for item in manifest.capabilities
    )


class SwarmTransport:
    """Verify discovery and claim traffic without making trading decisions."""

    def __init__(
        self,
        signed_manifest: SignedNodeManifest,
        *,
        receive_claim: ClaimReceiver | None = None,
        inbox: PeerClaimInbox | None = None,
        accepted_claim_schema_versions: frozenset[str] | None = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.signed_manifest = signed_manifest
        self._clock = clock
        self.inbox = inbox or PeerClaimInbox()
        self._receive_claim = receive_claim or self.inbox.receive
        self._accepted_claim_schema_versions = accepted_claim_schema_versions
        local = self.local_manifest()
        if not _has_capability(local, "claims.receive"):
            raise ValueError("The local manifest must advertise claims.receive version 1")

    def local_manifest(self) -> NodeManifest:
        return verify_signed_node_manifest(self.signed_manifest, at=self._clock())

    def replace_local_manifest(self, signed_manifest: SignedNodeManifest) -> None:
        """Renew the local manifest without changing this runtime's node identity."""

        # A renewal can arrive just after the previous manifest expires. Verify the
        # stored manifest at its own issue time so its content hash, signature, and
        # advertised identity are still authenticated without requiring it to remain
        # current. The replacement itself must be valid now.
        current = verify_signed_node_manifest(
            self.signed_manifest,
            at=self.signed_manifest.manifest.issued_at,
        )
        replacement = verify_signed_node_manifest(signed_manifest, at=self._clock())
        if replacement.node_id != current.node_id or replacement.public_key != current.public_key:
            raise ValueError("A manifest renewal cannot change the local node identity")
        if not _has_capability(replacement, "claims.receive"):
            raise ValueError("The replacement manifest must advertise claims.receive version 1")
        self.signed_manifest = signed_manifest

    def negotiate(self, request: PeerNegotiationRequest) -> PeerNegotiationResponse:
        local = self.local_manifest()
        try:
            peer = verify_signed_node_manifest(request.peer_manifest, at=self._clock())
        except ValueError as error:
            raise PeerExchangeError("Peer manifest verification failed") from error

        requested = set(request.requested_topics)
        accepted = tuple(
            sorted(requested & set(local.supported_topics) & set(peer.supported_topics))
        )
        rejected = tuple(sorted(requested - set(accepted)))
        compatible_schemas = _compatible_declarations(
            local.schema_versions,
            peer.schema_versions,
            name="rati.signed_claim",
        )
        if not _has_capability(peer, "claims.publish"):
            accepted = ()
            rejected = request.requested_topics
        if not compatible_schemas:
            accepted = ()
            rejected = request.requested_topics
        return PeerNegotiationResponse(
            local_node_id=local.node_id,
            peer_node_id=peer.node_id,
            accepted_topics=accepted,
            rejected_topics=rejected,
            compatible_claim_schemas=compatible_schemas,
        )

    async def accept_claim(self, exchange: ClaimExchangeRequest) -> ClaimExchangeReceipt:
        local = self.local_manifest()
        checked_at = self._clock()
        try:
            peer = verify_signed_node_manifest(exchange.peer_manifest, at=checked_at)
            claim = exchange.signed_claim.verify(at=checked_at)
        except ValueError as error:
            raise PeerExchangeError("Peer signature or validity verification failed") from error

        if not _has_capability(peer, "claims.publish"):
            raise PeerExchangeError("Peer does not advertise claims.publish version 1")
        if not _compatible_declarations(
            local.schema_versions,
            peer.schema_versions,
            name="rati.signed_claim",
        ):
            raise PeerExchangeError("Peer does not advertise a compatible signed-claim schema")
        if (
            exchange.topic not in local.supported_topics
            or exchange.topic not in peer.supported_topics
        ):
            raise PeerExchangeError("Topic was not negotiated by both peers")
        if (
            claim.claim.issuer_node_id != peer.node_id
            or claim.claim.issuer_public_key != peer.public_key
        ):
            raise PeerExchangeError("Claim issuer does not match the signed peer manifest")
        if (
            self._accepted_claim_schema_versions is not None
            and isinstance(claim.claim, RunnerObservationV1)
            and claim.claim.schema_version not in self._accepted_claim_schema_versions
        ):
            raise PeerExchangeError("Claim payload schema is not accepted locally")

        received = ReceivedPeerClaim(
            topic=exchange.topic,
            peer_manifest=peer,
            signed_claim=claim,
            received_at=checked_at,
        )
        result = self._receive_claim(received)
        if inspect.isawaitable(result):
            result = await result
        is_new = result is not False
        return ClaimExchangeReceipt(
            duplicate=not is_new,
            claim_id=claim.claim_id,
            local_node_id=local.node_id,
            peer_node_id=peer.node_id,
            topic=exchange.topic,
        )


async def _read_bounded_json(request: Request, *, maximum_bytes: int) -> bytes:
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type not in JSON_MEDIA_TYPES:
        raise HTTPException(status_code=415, detail="A supported JSON content type is required")
    raw_length = request.headers.get("content-length")
    if raw_length:
        try:
            content_length = int(raw_length)
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from error
        if content_length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if content_length > maximum_bytes:
            raise HTTPException(status_code=413, detail="Swarm message is too large")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise HTTPException(status_code=413, detail="Swarm message is too large")
    if not body:
        raise HTTPException(status_code=400, detail="Swarm message is empty")
    return bytes(body)


def _json_response(model: SwarmModel, *, status_code: int = 200) -> Response:
    return Response(
        content=canonical_json_bytes(model),
        status_code=status_code,
        media_type=MANIFEST_MEDIA_TYPE,
        headers={"X-Content-Type-Options": "nosniff"},
    )


def create_swarm_router(
    signed_manifest: SignedNodeManifest,
    *,
    receive_claim: ClaimReceiver | None = None,
    inbox: PeerClaimInbox | None = None,
    accepted_claim_schema_versions: frozenset[str] | None = None,
    clock: Callable[[], datetime] = _now,
) -> APIRouter:
    """Build routes that runtime wiring can mount on a FastAPI application."""

    transport = SwarmTransport(
        signed_manifest,
        receive_claim=receive_claim,
        inbox=inbox,
        accepted_claim_schema_versions=accepted_claim_schema_versions,
        clock=clock,
    )
    router = APIRouter()

    @router.get(WELL_KNOWN_PATH, include_in_schema=False)
    async def publish_manifest() -> Response:
        manifest = transport.local_manifest()
        seconds_left = max(0, int((manifest.expires_at - clock()).total_seconds()))
        return Response(
            content=transport.signed_manifest.to_wire_bytes(),
            media_type=MANIFEST_MEDIA_TYPE,
            headers={
                "Cache-Control": f"public, max-age={min(seconds_left, 300)}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post(NEGOTIATION_PATH, include_in_schema=False)
    async def negotiate(request: Request) -> Response:
        body = await _read_bounded_json(request, maximum_bytes=MAX_NEGOTIATION_BYTES)
        try:
            negotiation = PeerNegotiationRequest.model_validate_json(body)
            response = transport.negotiate(negotiation)
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail="Invalid peer negotiation") from error
        return _json_response(response)

    @router.post(CLAIM_EXCHANGE_PATH, include_in_schema=False)
    async def exchange_claim(request: Request) -> Response:
        body = await _read_bounded_json(request, maximum_bytes=MAX_CLAIM_EXCHANGE_BYTES)
        try:
            exchange = ClaimExchangeRequest.model_validate_json(body)
            receipt = await transport.accept_claim(exchange)
        except PeerClaimRejected as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        except (ValueError, TypeError) as error:
            raise HTTPException(status_code=400, detail="Invalid peer claim") from error
        return _json_response(receipt, status_code=202)

    # Runtime code can inspect the transport and its default inbox without a global registry.
    router.swarm_transport = transport  # type: ignore[attr-defined]
    return router


def _validate_discovery_origin(
    origin: str,
    *,
    allow_private_addresses: bool = False,
) -> SplitResult:
    if not 1 <= len(origin) <= 512:
        raise DiscoveryError("Discovery origin must contain 1 to 512 characters")
    if any(character.isspace() or ord(character) < 32 for character in origin) or "\\" in origin:
        raise DiscoveryError("Discovery origin contains unsafe characters")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError as error:
        raise DiscoveryError("Discovery origin is malformed") from error
    if parsed.scheme != "https" or not parsed.hostname:
        raise DiscoveryError("Discovery requires an https:// origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DiscoveryError("Discovery origin cannot include credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise DiscoveryError("Discovery origin cannot include a path")
    if port not in {None, 443}:
        raise DiscoveryError("Discovery only permits HTTPS port 443")

    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname or "%" in hostname:
        raise DiscoveryError("Discovery hostname is malformed")
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise DiscoveryError("Discovery hostname is malformed") from error
    is_local_name = hostname == "localhost" or hostname.endswith(_BLOCKED_HOST_SUFFIXES)
    if is_local_name and not allow_private_addresses:
        raise DiscoveryError("Local discovery hostnames are not allowed")
    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError as error:
        if "." not in hostname and not allow_private_addresses:
            raise DiscoveryError("Discovery requires a public fully qualified hostname") from error
    else:
        if not allow_private_addresses and not literal_address.is_global:
            raise DiscoveryError("Discovery IP address must be public")
        if (
            literal_address.is_unspecified
            or literal_address.is_multicast
            or literal_address.is_reserved
        ):
            raise DiscoveryError("Discovery IP address is not connectable")

    netloc = f"[{hostname}]" if ":" in hostname else hostname
    return SplitResult("https", netloc, "", "", "")


def well_known_manifest_url(origin: str, *, allow_private_addresses: bool = False) -> str:
    """Return the one allowed manifest URL for a permitted HTTPS origin."""

    parsed = _validate_discovery_origin(
        origin,
        allow_private_addresses=allow_private_addresses,
    )
    return urlunsplit(parsed._replace(path=WELL_KNOWN_PATH))


def _peer_transport_url(
    origin: str,
    path: str,
    *,
    allow_private_addresses: bool,
) -> str:
    parsed = _validate_discovery_origin(
        origin,
        allow_private_addresses=allow_private_addresses,
    )
    return urlunsplit(parsed._replace(path=path))


@dataclass(frozen=True, slots=True)
class _ResolvedAddress:
    family: socket.AddressFamily
    sockaddr: tuple[Any, ...]


def _resolve_addresses(
    hostname: str,
    port: int,
    *,
    allow_private_addresses: bool = False,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> tuple[_ResolvedAddress, ...]:
    try:
        answers = resolver(hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError as error:
        raise DiscoveryError("Discovery hostname could not be resolved") from error
    resolved: list[_ResolvedAddress] = []
    seen: set[tuple[int, str]] = set()
    for family, socktype, _proto, _canonname, sockaddr in answers:
        if family not in {socket.AF_INET, socket.AF_INET6} or socktype != socket.SOCK_STREAM:
            continue
        address = str(sockaddr[0])
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise DiscoveryError("DNS returned a malformed address") from error
        if not allow_private_addresses and not parsed_address.is_global:
            raise DiscoveryError("Discovery hostname resolves to a non-public address")
        if (
            parsed_address.is_unspecified
            or parsed_address.is_multicast
            or parsed_address.is_reserved
        ):
            raise DiscoveryError("Discovery hostname resolves to a non-connectable address")
        key = (int(family), address)
        if key not in seen:
            seen.add(key)
            resolved.append(_ResolvedAddress(family=family, sockaddr=sockaddr))
        if len(resolved) > MAX_RESOLVED_ADDRESSES:
            raise DiscoveryError("Discovery hostname returned too many addresses")
    if not resolved:
        raise DiscoveryError("Discovery hostname has no permitted TCP address")
    return tuple(resolved)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to pre-checked DNS answers while keeping TLS SNI and Host intact."""

    def __init__(
        self,
        host: str,
        addresses: Sequence[_ResolvedAddress],
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host=host, port=443, timeout=timeout, context=context)
        self._addresses = addresses
        self._deadline = time.monotonic() + timeout

    def _remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Discovery request timed out")
        return remaining

    def connect(self) -> None:
        last_error: OSError | None = None
        for address in self._addresses:
            raw_socket = socket.socket(address.family, socket.SOCK_STREAM)
            try:
                raw_socket.settimeout(self._remaining())
                raw_socket.connect(address.sockaddr)
                raw_socket.settimeout(self._remaining())
                self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
                self.sock.settimeout(self._remaining())
                return
            except OSError as error:
                last_error = error
                raw_socket.close()
        raise DiscoveryError("Could not connect to a checked peer address") from last_error


def _request_https_bytes(
    method: Literal["GET", "POST"],
    url: str,
    addresses: Sequence[_ResolvedAddress],
    *,
    body: bytes | None,
    expected_status: int,
    maximum_response_bytes: int,
    timeout_seconds: float,
) -> bytes:
    parsed = urlsplit(url)
    connection = _PinnedHTTPSConnection(
        parsed.hostname or "",
        addresses,
        timeout=timeout_seconds,
        context=ssl.create_default_context(),
    )
    try:
        headers = {
            "Accept": f"{MANIFEST_MEDIA_TYPE}, application/json",
            "User-Agent": "runner-watch-swarm/1",
        }
        if body is not None:
            headers["Content-Type"] = MANIFEST_MEDIA_TYPE
        connection.request(method, parsed.path, body=body, headers=headers)
        if connection.sock is not None:
            connection.sock.settimeout(connection._remaining())
        response = connection.getresponse()
        if response.status != expected_status:
            raise DiscoveryError(f"Peer returned HTTP {response.status}")
        media_type = (response.getheader("Content-Type") or "").partition(";")[0].strip().lower()
        if media_type not in JSON_MEDIA_TYPES:
            raise DiscoveryError("Peer returned an unsupported content type")
        if response.getheader("Content-Encoding"):
            raise DiscoveryError("Compressed peer responses are not accepted")
        raw_length = response.getheader("Content-Length")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError as error:
                raise DiscoveryError("Peer returned an invalid Content-Length") from error
            if content_length < 0 or content_length > maximum_response_bytes:
                raise DiscoveryError("Peer response is too large")
        if connection.sock is not None:
            connection.sock.settimeout(connection._remaining())
        payload = response.read(maximum_response_bytes + 1)
        if len(payload) > maximum_response_bytes:
            raise DiscoveryError("Peer response is too large")
        return payload
    except (OSError, http.client.HTTPException, TimeoutError) as error:
        raise DiscoveryError("Peer HTTPS request failed") from error
    finally:
        connection.close()


def _request_manifest_bytes(
    url: str,
    addresses: Sequence[_ResolvedAddress],
    *,
    timeout_seconds: float,
) -> bytes:
    return _request_https_bytes(
        "GET",
        url,
        addresses,
        body=None,
        expected_status=200,
        maximum_response_bytes=MAX_SIGNED_MANIFEST_BYTES,
        timeout_seconds=timeout_seconds,
    )


def _request_peer_bytes(
    url: str,
    addresses: Sequence[_ResolvedAddress],
    *,
    body: bytes,
    expected_status: int,
    maximum_response_bytes: int,
    timeout_seconds: float,
) -> bytes:
    return _request_https_bytes(
        "POST",
        url,
        addresses,
        body=body,
        expected_status=expected_status,
        maximum_response_bytes=maximum_response_bytes,
        timeout_seconds=timeout_seconds,
    )


def fetch_signed_manifest(
    origin: str,
    *,
    at: datetime | None = None,
    timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    allow_private_addresses: bool = False,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    requester: Callable[..., bytes] | None = None,
) -> SignedNodeManifest:
    """Fetch and verify a manifest; private targets require an explicit opt-in."""

    _validate_timeout(timeout_seconds)
    url = well_known_manifest_url(
        origin,
        allow_private_addresses=allow_private_addresses,
    )
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    addresses = _resolve_addresses(
        hostname,
        443,
        allow_private_addresses=allow_private_addresses,
        resolver=resolver,
    )
    request = requester or _request_manifest_bytes
    payload = request(url, addresses, timeout_seconds=timeout_seconds)
    try:
        signed = SignedNodeManifest.from_wire_bytes(payload)
        verify_signed_node_manifest(signed, at=at)
    except ValueError as error:
        raise DiscoveryError("Discovery manifest verification failed") from error
    return signed


def _validate_timeout(timeout_seconds: float) -> None:
    if not 0 < timeout_seconds <= MAX_FETCH_TIMEOUT_SECONDS:
        raise ValueError(f"timeout_seconds must be between 0 and {MAX_FETCH_TIMEOUT_SECONDS:g}")


def _outbound_target(
    origin: str,
    path: str,
    *,
    allow_private_addresses: bool,
    resolver: Callable[..., list[tuple[Any, ...]]],
) -> tuple[str, tuple[_ResolvedAddress, ...]]:
    url = _peer_transport_url(
        origin,
        path,
        allow_private_addresses=allow_private_addresses,
    )
    hostname = urlsplit(url).hostname or ""
    addresses = _resolve_addresses(
        hostname,
        443,
        allow_private_addresses=allow_private_addresses,
        resolver=resolver,
    )
    return url, addresses


def negotiate_with_peer(
    origin: str,
    local_manifest: SignedNodeManifest,
    requested_topics: Sequence[str],
    *,
    expected_peer_node_id: str | None = None,
    at: datetime | None = None,
    timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    allow_private_addresses: bool = False,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    requester: Callable[..., bytes] | None = None,
) -> PeerNegotiationResponse:
    """Negotiate topics over pinned HTTPS and verify the peer's bounded response."""

    _validate_timeout(timeout_seconds)
    checked_local = verify_signed_node_manifest(local_manifest, at=at)
    if not _has_capability(checked_local, "claims.publish"):
        raise PeerExchangeError("The local manifest must advertise claims.publish version 1")
    request = PeerNegotiationRequest(
        peer_manifest=local_manifest,
        requested_topics=tuple(requested_topics),
    )
    if any(topic not in checked_local.supported_topics for topic in request.requested_topics):
        raise PeerExchangeError("The local manifest does not advertise every requested topic")
    body = canonical_json_bytes(request)
    if len(body) > MAX_NEGOTIATION_BYTES:
        raise PeerExchangeError("Peer negotiation is too large")
    url, addresses = _outbound_target(
        origin,
        NEGOTIATION_PATH,
        allow_private_addresses=allow_private_addresses,
        resolver=resolver,
    )
    send = requester or _request_peer_bytes
    try:
        payload = send(
            url,
            addresses,
            body=body,
            expected_status=200,
            maximum_response_bytes=MAX_NEGOTIATION_BYTES,
            timeout_seconds=timeout_seconds,
        )
        response = PeerNegotiationResponse.model_validate_json(payload)
        if canonical_json_bytes(response) != payload:
            raise ValueError("Negotiation response is not canonical JSON")
    except (DiscoveryError, ValueError, TypeError) as error:
        raise PeerExchangeError("Peer negotiation failed") from error
    if response.peer_node_id != checked_local.node_id:
        raise PeerExchangeError("Negotiation response names the wrong requesting node")
    if expected_peer_node_id is not None and response.local_node_id != expected_peer_node_id:
        raise PeerExchangeError("Negotiation response names an unexpected peer node")
    accepted = set(response.accepted_topics)
    rejected = set(response.rejected_topics)
    requested = set(request.requested_topics)
    if accepted & rejected or accepted | rejected != requested:
        raise PeerExchangeError("Negotiation response does not partition the requested topics")
    if response.accepted_topics and not _compatible_declarations(
        checked_local.schema_versions,
        response.compatible_claim_schemas,
        name="rati.signed_claim",
    ):
        raise PeerExchangeError("Negotiation response does not include a compatible claim schema")
    return response


def post_claim_to_peer(
    origin: str,
    local_manifest: SignedNodeManifest,
    signed_claim: SignedClaimV1,
    topic: str,
    *,
    expected_peer_node_id: str | None = None,
    at: datetime | None = None,
    timeout_seconds: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    allow_private_addresses: bool = False,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    requester: Callable[..., bytes] | None = None,
) -> ClaimExchangeReceipt:
    """Post one signed claim over pinned HTTPS and verify its bounded receipt."""

    _validate_timeout(timeout_seconds)
    checked_local = verify_signed_node_manifest(local_manifest, at=at)
    checked_claim = signed_claim.verify(at=at)
    if not _has_capability(checked_local, "claims.publish"):
        raise PeerExchangeError("The local manifest must advertise claims.publish version 1")
    if (
        checked_claim.claim.issuer_node_id != checked_local.node_id
        or checked_claim.claim.issuer_public_key != checked_local.public_key
    ):
        raise PeerExchangeError("The claim issuer does not match the local manifest")
    if topic not in checked_local.supported_topics:
        raise PeerExchangeError("The local manifest does not advertise this topic")

    exchange = ClaimExchangeRequest(
        topic=topic,
        peer_manifest=local_manifest,
        signed_claim=checked_claim,
    )
    body = canonical_json_bytes(exchange)
    if len(body) > MAX_CLAIM_EXCHANGE_BYTES:
        raise PeerExchangeError("Peer claim exchange is too large")
    url, addresses = _outbound_target(
        origin,
        CLAIM_EXCHANGE_PATH,
        allow_private_addresses=allow_private_addresses,
        resolver=resolver,
    )
    send = requester or _request_peer_bytes
    try:
        payload = send(
            url,
            addresses,
            body=body,
            expected_status=202,
            maximum_response_bytes=MAX_NEGOTIATION_BYTES,
            timeout_seconds=timeout_seconds,
        )
        receipt = ClaimExchangeReceipt.model_validate_json(payload)
        if canonical_json_bytes(receipt) != payload:
            raise ValueError("Claim receipt is not canonical JSON")
    except (DiscoveryError, ValueError, TypeError) as error:
        raise PeerExchangeError("Peer claim post failed") from error
    if receipt.peer_node_id != checked_local.node_id:
        raise PeerExchangeError("Claim receipt names the wrong sending node")
    if receipt.claim_id != checked_claim.claim_id or receipt.topic != topic:
        raise PeerExchangeError("Claim receipt does not match the sent claim")
    if expected_peer_node_id is not None and receipt.local_node_id != expected_peer_node_id:
        raise PeerExchangeError("Claim receipt names an unexpected peer node")
    return receipt


__all__ = [
    "CLAIM_EXCHANGE_PATH",
    "ClaimExchangeReceipt",
    "ClaimExchangeRequest",
    "DEFAULT_FETCH_TIMEOUT_SECONDS",
    "DiscoveryError",
    "MANIFEST_MEDIA_TYPE",
    "MAX_CLAIM_EXCHANGE_BYTES",
    "MAX_NEGOTIATION_BYTES",
    "NEGOTIATION_PATH",
    "PeerClaimInbox",
    "PeerClaimRejected",
    "PeerExchangeError",
    "PeerNegotiationRequest",
    "PeerNegotiationResponse",
    "ReceivedPeerClaim",
    "SwarmTransport",
    "WELL_KNOWN_PATH",
    "create_swarm_router",
    "fetch_signed_manifest",
    "negotiate_with_peer",
    "post_claim_to_peer",
    "well_known_manifest_url",
]
