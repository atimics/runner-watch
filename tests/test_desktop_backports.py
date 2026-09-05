from __future__ import annotations

import runpy
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(ROOT / "scripts/verify_desktop_backports.py"))


class GlibBackportTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.desktop = self.root / "desktop/src-tauri"
        self.desktop.mkdir(parents=True)
        for name in ("vendor/glib", "vendor/urlpattern", "patches"):
            shutil.copytree(ROOT / "desktop/src-tauri" / name, self.desktop / name)
        for name in ("Cargo.toml", "Cargo.lock"):
            shutil.copyfile(ROOT / "desktop/src-tauri" / name, self.desktop / name)

    def verify(self) -> int:
        return MODULE["verify_backport"](self.root)

    def test_published_source_with_upstream_fix_passes(self) -> None:
        self.assertEqual(self.verify(), 121)

    def test_partial_pointer_fix_fails(self) -> None:
        source = self.desktop / "vendor/glib/src/variant_iter.rs"
        source.write_bytes(
            source.read_bytes().replace(b"                &mut p,", b"                &p,")
        )
        with self.assertRaisesRegex(ValueError, "exact upstream changes"):
            self.verify()

    def test_unrelated_source_change_fails(self) -> None:
        source = self.desktop / "vendor/glib/src/lib.rs"
        source.write_bytes(source.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch: src/lib.rs"):
            self.verify()

    def test_extra_source_file_fails(self) -> None:
        (self.desktop / "vendor/glib/src/extra.rs").write_text("pub fn extra() {}\n")
        with self.assertRaisesRegex(ValueError, "source file set"):
            self.verify()

    def test_registry_resolution_fails(self) -> None:
        lock = self.desktop / "Cargo.lock"
        lock.write_text(
            lock.read_text().replace(
                'name = "glib"\n',
                'name = "glib"\nsource = "registry+https://github.com/rust-lang/crates.io-index"\n',
            )
        )
        with self.assertRaisesRegex(ValueError, "local glib 0.18.5 backport"):
            self.verify()

    def test_urlpattern_upstream_tokenizer_passes(self) -> None:
        self.assertEqual(MODULE["verify_backport"](self.root, "urlpattern"), 19)

    def test_urlpattern_partial_replacement_fails(self) -> None:
        source = self.desktop / "vendor/urlpattern/src/tokenizer.rs"
        source.write_text(
            source.read_text().replace(
                "ID_START.contains(code_point)", "unic_ucd_ident::is_id_start(code_point)"
            )
        )
        with self.assertRaisesRegex(ValueError, "exact upstream changes"):
            MODULE["verify_backport"](self.root, "urlpattern")

    def test_urlpattern_unrelated_source_change_fails(self) -> None:
        source = self.desktop / "vendor/urlpattern/src/lib.rs"
        source.write_bytes(source.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch: src/lib.rs"):
            MODULE["verify_backport"](self.root, "urlpattern")

    def test_version_metadata_change_fails(self) -> None:
        source = self.desktop / "vendor/glib/Cargo.toml"
        source.write_text(source.read_text().replace('version = "0.18.5"', 'version = "0.20.0"'))
        with self.assertRaisesRegex(ValueError, "checksum mismatch: Cargo.toml"):
            self.verify()
