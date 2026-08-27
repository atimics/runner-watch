from __future__ import annotations

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


def request_client_ip(request: Any, *, trust_fly_client_ip: bool) -> str:
    direct = request.client.host if request.client else "unknown"
    if not trust_fly_client_ip:
        return direct
    forwarded = request.headers.get("fly-client-ip", "").strip()
    try:
        return ip_address(forwarded).compressed
    except ValueError:
        return direct
