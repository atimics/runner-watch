from __future__ import annotations

import hashlib
import json
import runpy
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_desktop_release.py"
MODULE = runpy.run_path(str(SCRIPT))
COMMIT = "1234567890abcdef1234567890abcdef12345678"


class DesktopReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.artifacts = self.root / "artifacts"
        self.output = self.root / "release"
        (self.root / "desktop/src-tauri").mkdir(parents=True)
        (self.root / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        (self.root / "desktop/package.json").write_text('{"version": "1.0.0"}')
        (self.root / "desktop/src-tauri/tauri.conf.json").write_text('{"version": "1.0.0"}')
        (self.root / "desktop/src-tauri/Cargo.toml").write_text(
            '[package]\nversion = "1.0.0"\n'
        )
        self.installers = []
        for platform, _architecture, extension, template in MODULE["ASSETS"]:
            path = self.artifacts / f"rati-swarm-{platform}" / extension / template.format(
                version="1.0.0"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"Installer fixture for {platform} {extension}".encode())
            self.installers.append(path)

    def prepare(self, *, tag: str = "v1.0.0", commit: str = COMMIT) -> dict[str, object]:
        return MODULE["prepare_release"](self.root, self.artifacts, self.output, tag, commit)

    def test_release_has_five_installers_and_verified_commit_manifest(self) -> None:
        manifest = self.prepare()

        self.assertEqual(manifest["version"], "1.0.0")
        self.assertEqual(manifest["tag"], "v1.0.0")
        self.assertEqual(manifest["commit"], COMMIT)
        self.assertEqual(len(manifest["assets"]), 5)
        self.assertEqual(
            {asset["name"] for asset in manifest["assets"]},
            {
                "RATi-Runners-1.0.0-linux-x86_64.deb",
                "RATi-Runners-1.0.0-linux-x86_64.AppImage",
                "RATi-Runners-1.0.0-macos-aarch64.dmg",
                "RATi-Runners-1.0.0-windows-x86_64.msi",
                "RATi-Runners-1.0.0-windows-x86_64.exe",
            },
        )
        self.assertEqual(json.loads((self.output / "release-manifest.json").read_text()), manifest)
        checksum_files = set()
        for line in (self.output / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), digest)
            checksum_files.add(name)
        self.assertEqual(
            checksum_files,
            {asset["name"] for asset in manifest["assets"]} | {"release-manifest.json"},
        )
        for asset in manifest["assets"]:
            payload = (self.artifacts / asset["source"]).read_bytes()
            self.assertEqual((self.output / asset["name"]).read_bytes(), payload)
            self.assertEqual(asset["sha256"], hashlib.sha256(payload).hexdigest())
            self.assertEqual(asset["size"], len(payload))

    def test_missing_platform_stops_release_before_output(self) -> None:
        self.installers[2].unlink()
        with self.assertRaisesRegex(ValueError, "Expected one dmg installer"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_duplicate_installer_stops_release(self) -> None:
        duplicate = self.installers[0].parent / "old_0.1.0_amd64.deb"
        duplicate.write_bytes(b"Old build")
        with self.assertRaisesRegex(ValueError, "Expected one deb installer"):
            self.prepare()

    def test_wrong_installer_version_stops_release(self) -> None:
        installer = self.installers[0]
        installer.rename(installer.with_name(installer.name.replace("1.0.0", "0.1.0")))
        with self.assertRaisesRegex(ValueError, "Expected installer"):
            self.prepare()

    def test_empty_installer_stops_release(self) -> None:
        self.installers[0].write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "nonempty regular file"):
            self.prepare()

    def test_existing_output_stays_intact(self) -> None:
        self.output.mkdir()
        saved = self.output / "existing.exe"
        saved.write_bytes(b"Saved release")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            self.prepare()
        self.assertEqual(saved.read_bytes(), b"Saved release")

    def test_package_version_mismatch_stops_release(self) -> None:
        (self.root / "desktop/package.json").write_text('{"version": "0.1.0"}')
        with self.assertRaisesRegex(ValueError, "Package versions differ"):
            self.prepare()

    def test_tag_must_match_package_version(self) -> None:
        for tag in ("v0.1.0", "1.0.0", "v1.0.0-beta.1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "must match"):
                self.prepare(tag=tag)

    def test_full_commit_sha_is_required(self) -> None:
        for commit in ("1234567", "main", "g" * 40):
            with self.subTest(commit=commit), self.assertRaisesRegex(ValueError, "full lowercase"):
                self.prepare(commit=commit)


if __name__ == "__main__":
    unittest.main()
