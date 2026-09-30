"""trust.rati.chat: RATi audits, in two phases.

A draft is a status page that changes while an audit is under way: counts, open
gates, reviewer wallets and a commitment to the current record. It never holds
code, a repository or finding text. The report itself sits beside it as a
sealed vault, one HTML file that opens only for the reviewers' Solana wallets.

A final is one immutable bundle: the plain report, its signed attestation and a
manifest of file hashes. Its address is the Arweave transaction it was uploaded
as (or, before that, the SHA-256 of its manifest). The hashes are recomputed
here on every read so a reader sees whether the files still match.

Both are written by `ratiaudit publish` (atimics/ratiaudit) and committed under
assets/audits/draft/<uuid>/ and assets/audits/final/<address>/.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).parent / "assets" / "audits"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
# An Arweave transaction id is 43 base64url characters.
# Before upload a bundle is named by the SHA-256 of its manifest.
BUNDLE = re.compile(r"^(?:[0-9a-f]{64}|[A-Za-z0-9_-]{43})$")
FINAL_FILES = {
    "record.json": "application/json",
    "report.md": "text/markdown",
    "design.md": "text/markdown",
    "costs.md": "text/markdown",
    "attestation.json": "application/json",
    "attestation.json.sig": "text/plain",
    "allowed_signers": "text/plain",
    "manifest.json": "application/json",
}
# The vault page loads tweetnacl from a CDN and runs inline scripts,
# and its report frame inherits this policy.
VAULT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com; "
    "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src data:; connect-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _inside(*parts: str) -> Path | None:
    """A path under ASSETS, or None if it would leave it. Every request reads through here."""

    root = os.path.realpath(ASSETS)
    path = os.path.realpath(os.path.join(root, *parts))
    return Path(path) if path.startswith(root + os.sep) else None


def _read(*parts: str) -> str | None:
    path = _inside(*parts)
    try:
        return path.read_text() if path else None
    except OSError:
        return None


def _bytes(*parts: str) -> bytes | None:
    path = _inside(*parts)
    try:
        return path.read_bytes() if path else None
    except OSError:
        return None


def draft(audit_uuid: str) -> dict[str, Any] | None:
    """The public status of a draft, or None."""

    if not UUID.fullmatch(audit_uuid):
        return None
    text = _read("draft", audit_uuid, "status.json")
    if text is None:
        return None
    try:
        status = json.loads(text)
    except ValueError:
        return None
    vault = _inside("draft", audit_uuid, "vault.html")
    status["has_vault"] = bool(vault and vault.is_file())
    return status


def draft_vault(audit_uuid: str) -> str | None:
    if not UUID.fullmatch(audit_uuid):
        return None
    return _read("draft", audit_uuid, "vault.html")


def final(bundle_id: str) -> dict[str, Any] | None:
    """A final bundle with every file hash recomputed, or None."""

    if not BUNDLE.fullmatch(bundle_id):
        return None
    manifest_text = _read("final", bundle_id, "manifest.json")
    if manifest_text is None:
        return None
    try:
        manifest = json.loads(manifest_text)
    except ValueError:
        return None
    files, verified = [], True
    for name, expected in sorted(manifest.get("files", {}).items()):
        data = _bytes("final", bundle_id, name) if name in FINAL_FILES else None
        ok = data is not None and hashlib.sha256(data).hexdigest() == expected
        verified &= ok
        files.append({"name": name, "sha256": expected, "ok": ok})
    attestation = None
    try:
        attestation = json.loads(_read("final", bundle_id, "attestation.json") or "")
    except ValueError:
        verified = False
    return {
        "id": bundle_id,
        "bundle_hash": hashlib.sha256(manifest_text.encode()).hexdigest(),
        "manifest": manifest,
        "attestation": attestation,
        "files": files,
        "verified": verified,
    }


def final_file(bundle_id: str, name: str) -> tuple[bytes, str] | None:
    if not BUNDLE.fullmatch(bundle_id) or name not in FINAL_FILES:
        return None
    data = _bytes("final", bundle_id, name)
    return (data, FINAL_FILES[name]) if data is not None else None
