from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from pathlib import Path

ARCHIVE_SHA256 = "233daaf6e83ae6a12a52055f568f9d7cf4671dabb78ff9560ab6da230ce00ee5"
UPSTREAM_FIX = (
    "https://github.com/gtk-rs/gtk-rs-core/commit/b5a4071e439bef2b5eea76c3aa25e5ae84839e34"
)
REPLACEMENTS = (
    (
        b"let mut p: *mut libc::c_char = std::ptr::null_mut();",
        b"let p: *mut libc::c_char = std::ptr::null_mut();",
    ),
    (b"                &mut p,\n", b"                &p,\n"),
)


def verify_backport(root: Path) -> int:
    desktop = root / "desktop/src-tauri"
    vendor = desktop / "vendor/glib"
    metadata = json.loads((desktop / "patches/glib-0.18.5.json").read_text())
    expected = {
        "package": "glib",
        "version": "0.18.5",
        "license": "MIT",
        "archive": "https://static.crates.io/crates/glib/glib-0.18.5.crate",
        "archive_sha256": ARCHIVE_SHA256,
        "upstream_fix": UPSTREAM_FIX,
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            raise ValueError(f"GLib provenance mismatch: {field}")
    files = metadata["files"]
    actual = {path.relative_to(vendor).as_posix() for path in vendor.rglob("*") if path.is_file()}
    if actual != set(files):
        raise ValueError("GLib source file set differs from the published crate")
    for name, digest in files.items():
        payload = (vendor / name).read_bytes()
        if name == "src/variant_iter.rs":
            for patched, original in REPLACEMENTS:
                if payload.count(patched) != 1:
                    raise ValueError("GLib VariantStrIter requires both upstream pointer changes")
                payload = payload.replace(patched, original, 1)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"GLib published source checksum mismatch: {name}")
    manifest = tomllib.loads((desktop / "Cargo.toml").read_text())
    if manifest.get("patch", {}).get("crates-io", {}).get("glib") != {"path": "vendor/glib"}:
        raise ValueError("Desktop must resolve GLib through the verified local patch")
    lock = tomllib.loads((desktop / "Cargo.lock").read_text())
    packages = [package for package in lock["package"] if package["name"] == "glib"]
    if len(packages) != 1 or packages[0]["version"] != "0.18.5" or "source" in packages[0]:
        raise ValueError("Desktop lock must resolve the local GLib 0.18.5 backport")
    return len(files)


if __name__ == "__main__":
    try:
        count = verify_backport(Path(__file__).resolve().parents[1])
    except (OSError, ValueError, KeyError) as error:
        print(f"GLib backport verification failed: {error}", file=sys.stderr)
        sys.exit(1)
    print(f"Verified {count} published GLib files with the two upstream pointer changes")
