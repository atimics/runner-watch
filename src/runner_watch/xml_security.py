from __future__ import annotations

from typing import Any

from defusedxml import ElementTree as DefusedElementTree

MAX_XML_BYTES = 5 * 1024 * 1024


class PayloadTooLargeError(ValueError):
    pass


def read_limited(response: Any, *, max_bytes: int) -> bytes:

    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise PayloadTooLargeError(f"Response exceeds the {max_bytes}-byte limit")
    return body


def safe_xml_fromstring(
    payload: str | bytes,
    *,
    max_bytes: int = MAX_XML_BYTES,
) -> Any:
    size = len(payload) if isinstance(payload, bytes) else len(payload.encode("utf-8"))
    if size > max_bytes:
        raise PayloadTooLargeError(f"XML response exceeds the {max_bytes}-byte limit")
    return DefusedElementTree.fromstring(payload)
