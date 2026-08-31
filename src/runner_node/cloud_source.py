from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

RATI_CLOUD_ORIGIN = "https://runners.rati.chat"
MAX_RESPONSE_BYTES = 5_000_000


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def normalize_scanner_url(value: str) -> str:
    parsed = urlparse(value.strip())
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("Remote scanners must use HTTPS; loopback scanners may use HTTP")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Enter a scanner origin without credentials, a query, or a fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("Enter the scanner origin without an API path")
    return value.strip().rstrip("/")


def _get_json(url: str, token: str = "") -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "RATi-Swarm/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with build_opener(_NoRedirects()).open(request, timeout=12) as response:
            if response.headers.get_content_type() != "application/json":
                raise RuntimeError("Scanner source returned an invalid response")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("Scanner source rejected the access token") from exc
        raise RuntimeError(f"Scanner source returned {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Scanner source could not be reached") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Scanner source returned too much data")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Scanner source returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Scanner source returned an invalid payload")
    return payload


def _bounded_receipts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    receipts = payload.get("receipts")
    if not isinstance(receipts, list):
        raise RuntimeError("Scanner source returned an invalid scan list")
    receipts = payload["receipts"][:20]
    if not all(
        isinstance(receipt, dict)
        and receipt.get("source") == "live"
        and isinstance(receipt.get("rows"), list)
        and len(receipt["rows"]) <= 100
        for receipt in receipts
    ):
        raise RuntimeError("Scanner source returned an invalid scan receipt")
    return receipts


class RemoteScannerSource:
    """Reads bounded scan receipts from a configured scanner node."""

    def scans(self, url: str, token: str = "") -> list[dict[str, Any]]:
        origin = normalize_scanner_url(url)
        return _bounded_receipts(_get_json(f"{origin}/api/v1/scans", token))


class RatiCloudSource:
    """Normalizes the preconfigured free RATi feed into one source receipt."""

    def scans(self) -> list[dict[str, Any]]:
        payload = _get_json(f"{RATI_CLOUD_ORIGIN}/api/pulse?offset=0&limit=20")
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise RuntimeError("RATi Cloud returned an invalid source feed")
        normalized = []
        for row in rows[:20]:
            if not isinstance(row, dict) or not str(row.get("ticker") or "").strip():
                continue
            confidence = row.get("case_confidence")
            try:
                score = float(confidence) * 100 if confidence is not None else 0.0
            except (TypeError, ValueError):
                score = 0.0
            normalized.append(
                {
                    "ticker": str(row["ticker"]).strip().upper(),
                    "score": score,
                    "rug_score": float(row.get("rug_score") or 0),
                    "rug_level": str(row.get("rug_level") or "UNKNOWN"),
                    "trade_state": str(row.get("trade_state") or row.get("pulse_label") or "WATCH"),
                    "price": float(row.get("price") or 0),
                    "change_pct": float(row.get("change_pct") or 0),
                    "relative_volume": row.get("relative_volume"),
                    "state_reason": str(
                        row.get("directional_thesis")
                        or row.get("case_thesis")
                        or "RATi source candidate"
                    ),
                }
            )
        if not normalized:
            return []
        finished_at = str(payload.get("updated_at") or datetime.now(UTC).isoformat())
        return [
            {
                "id": f"rati-cloud-{finished_at}",
                "status": "complete",
                "source": "live",
                "finished_at": finished_at,
                "elapsed_seconds": 0.0,
                "requested_symbols": len(normalized),
                "liquid_symbols": len(normalized),
                "scanned_symbols": len(normalized),
                "rows": normalized,
                "warnings": [],
            }
        ]
