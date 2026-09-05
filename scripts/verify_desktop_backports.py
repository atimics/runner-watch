from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from pathlib import Path

BACKPORTS = {
    "glib": {
        "version": "0.18.5",
        "archive_sha256": "233daaf6e83ae6a12a52055f568f9d7cf4671dabb78ff9560ab6da230ce00ee5",
        "upstream_fix": "https://github.com/gtk-rs/gtk-rs-core/commit/b5a4071e439bef2b5eea76c3aa25e5ae84839e34",
        "replacements": {
            "src/variant_iter.rs": (
                (
                    b"let mut p: *mut libc::c_char = std::ptr::null_mut();",
                    b"let p: *mut libc::c_char = std::ptr::null_mut();",
                ),
                (b"                &mut p,\n", b"                &p,\n"),
            ),
        },
    },
    "urlpattern": {
        "version": "0.3.0",
        "archive_sha256": "70acd30e3aa1450bc2eece896ce2ad0d178e9c079493819301573dae3c37ba6d",
        "upstream_fix": "https://github.com/denoland/rust-urlpattern/commit/b047afee9b901e19928f87470ba722bbac05a27f",
        "replacements": {
            "Cargo.toml": (
                (
                    b'[dependencies.icu_properties]\nversion = "2"\n',
                    b'[dependencies.unic-ucd-ident]\nversion = "0.9.0"\nfeatures = ["id"]\n',
                ),
            ),
            "Cargo.toml.orig": (
                (
                    b'icu_properties = "2"\n',
                    b'unic-ucd-ident = { version = "0.9.0", features = ["id"] }\n',
                ),
            ),
            "src/tokenizer.rs": (
                (
                    b"use icu_properties::{\n  props::{IdContinue, IdStart},\n"
                    b"  CodePointSetDataBorrowed,\n};\n",
                    b"",
                ),
                (
                    b"static ID_START: CodePointSetDataBorrowed<'_> =\n"
                    b"  CodePointSetDataBorrowed::new::<IdStart>();\n"
                    b"static ID_CONTINUE: CodePointSetDataBorrowed<'_> =\n"
                    b"  CodePointSetDataBorrowed::new::<IdContinue>();\n\n",
                    b"",
                ),
                (b"ID_START.contains(code_point)", b"unic_ucd_ident::is_id_start(code_point)"),
                (
                    b"ID_CONTINUE.contains(code_point)",
                    b"unic_ucd_ident::is_id_continue(code_point)",
                ),
            ),
        },
    },
}


def verify_backport(root: Path, package: str = "glib") -> int:
    spec = BACKPORTS[package]
    version = spec["version"]
    desktop = root / "desktop/src-tauri"
    vendor = desktop / "vendor" / package
    metadata = json.loads((desktop / "patches" / f"{package}-{version}.json").read_text())
    expected = {
        "package": package,
        "version": version,
        "license": "MIT",
        "archive": f"https://static.crates.io/crates/{package}/{package}-{version}.crate",
        "archive_sha256": spec["archive_sha256"],
        "upstream_fix": spec["upstream_fix"],
    }
    for field, value in expected.items():
        if metadata.get(field) != value:
            raise ValueError(f"{package} provenance mismatch: {field}")
    files = metadata["files"]
    actual = {path.relative_to(vendor).as_posix() for path in vendor.rglob("*") if path.is_file()}
    if actual != set(files):
        raise ValueError(f"{package} source file set differs from the published crate")
    for name, digest in files.items():
        payload = (vendor / name).read_bytes()
        for patched, original in spec["replacements"].get(name, ()):
            if payload.count(patched) != 1:
                raise ValueError(f"{package} requires the exact upstream changes in {name}")
            payload = payload.replace(patched, original, 1)
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"{package} published source checksum mismatch: {name}")
    manifest = tomllib.loads((desktop / "Cargo.toml").read_text())
    if manifest.get("patch", {}).get("crates-io", {}).get(package) != {"path": f"vendor/{package}"}:
        raise ValueError(f"Desktop must resolve {package} through the verified local patch")
    lock = tomllib.loads((desktop / "Cargo.lock").read_text())
    packages = [entry for entry in lock["package"] if entry["name"] == package]
    if len(packages) != 1 or packages[0]["version"] != version or "source" in packages[0]:
        raise ValueError(f"Desktop lock must resolve the local {package} {version} backport")
    return len(files)


if __name__ == "__main__":
    try:
        for package in BACKPORTS:
            count = verify_backport(Path(__file__).resolve().parents[1], package)
            print(f"Verified {count} published {package} files with the upstream backport")
    except (OSError, ValueError, KeyError) as error:
        print(f"Desktop backport verification failed: {error}", file=sys.stderr)
        sys.exit(1)
