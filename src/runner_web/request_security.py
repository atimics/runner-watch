from __future__ import annotations

import hmac
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse


def safe_next_path(value: str | None) -> str:
    candidate = str(value or "/")
    if (
        not candidate.startswith("/")
        or "\\" in candidate
        or any(ord(character) < 32 for character in candidate)
    ):
        return "/"
    parsed = urlparse(candidate)
    return candidate if not parsed.scheme and not parsed.netloc else "/"


def edge_proxy_authenticated(request: Any, *, edge_proxy_secret: str) -> bool:
    """Return true only for requests carrying the shared edge-to-origin secret."""

    if not edge_proxy_secret:
        return False
    supplied = request.headers.get("x-rati-edge-secret", "")
    return bool(supplied) and hmac.compare_digest(supplied, edge_proxy_secret)


def _valid_ip(value: str) -> str | None:
    try:
        return ip_address(value.strip()).compressed
    except ValueError:
        return None


def request_client_ip(
    request: Any,
    *,
    trust_fly_client_ip: bool,
    edge_proxy_secret: str = "",
) -> str:
    direct = request.client.host if request.client else "unknown"
    if edge_proxy_authenticated(request, edge_proxy_secret=edge_proxy_secret):
        return _valid_ip(request.headers.get("x-rati-client-ip", "")) or direct
    if not trust_fly_client_ip:
        return direct
    return _valid_ip(request.headers.get("fly-client-ip", "")) or direct
