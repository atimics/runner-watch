"""The service runs the RATi Rules it publishes: the vendored package is the
version in force, byte for byte, by its digest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import ratitrust

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/runner_web/assets"


def vendored_digest() -> str:
    files = sorted(
        path
        for path in (ROOT / "src/ratitrust").rglob("*")
        if path.suffix in {".py", ".toml", ".json"}
    )
    manifest = "".join(
        f"{path.relative_to(ROOT).as_posix()} {hashlib.sha256(path.read_bytes()).hexdigest()}\n"
        for path in files
    )
    return hashlib.sha256(manifest.encode()).hexdigest()


def test_the_vendored_rules_are_the_version_in_force():
    current = json.loads((ASSETS / "trust-rules-current.json").read_text())

    assert ratitrust.__version__ == current["version"]
    assert vendored_digest() == current["digest"], (
        "src/ratitrust differs from the rules in force: copy the released ratitrust "
        "src/ratitrust here and publish its record, or undo the local change"
    )
