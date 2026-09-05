from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = (
    ("linux", "x86_64", "deb", "RATi Runners_{version}_amd64.deb"),
    ("linux", "x86_64", "AppImage", "RATi Runners_{version}_amd64.AppImage"),
    ("macos", "aarch64", "dmg", "RATi Runners_{version}_aarch64.dmg"),
    ("windows", "x86_64", "msi", "RATi Runners_{version}_x64_en-US.msi"),
    ("windows", "x86_64", "exe", "RATi Runners_{version}_x64-setup.exe"),
)


def _runtime_version(path: Path) -> str:
    assignments = [
        node
        for node in ast.parse(path.read_text()).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    if len(assignments) != 1:
        raise ValueError(f"Expected one runtime __version__ assignment in {path}")
    value = ast.literal_eval(assignments[0].value)
    if not isinstance(value, str):
        raise ValueError(f"Expected a string runtime version in {path}")
    return value


def _locked_version(path: Path, package_name: str) -> str:
    versions = [
        package["version"]
        for package in tomllib.loads(path.read_text())["package"]
        if package["name"] == package_name
    ]
    if len(versions) != 1:
        raise ValueError(f"Expected one locked {package_name} package in {path}")
    return versions[0]


def check_version(root: Path, tag: str | None = None) -> str:
    versions = {
        "pyproject.toml": tomllib.loads((root / "pyproject.toml").read_text())["project"][
            "version"
        ],
        "desktop/package.json": json.loads((root / "desktop/package.json").read_text())[
            "version"
        ],
        "desktop/src-tauri/tauri.conf.json": json.loads(
            (root / "desktop/src-tauri/tauri.conf.json").read_text()
        )["version"],
        "desktop/src-tauri/Cargo.toml": tomllib.loads(
            (root / "desktop/src-tauri/Cargo.toml").read_text()
        )["package"]["version"],
        "src/runner_watch/__init__.py": _runtime_version(root / "src/runner_watch/__init__.py"),
        "uv.lock": _locked_version(root / "uv.lock", "runner-watch"),
        "desktop/src-tauri/Cargo.lock": _locked_version(
            root / "desktop/src-tauri/Cargo.lock", "rati-swarm"
        ),
    }
    version = versions["pyproject.toml"]
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version):
        raise ValueError(f"Expected a stable release version, got {version!r}")
    if any(value != version for value in versions.values()):
        raise ValueError(f"Package versions differ: {versions}")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Tag {tag!r} must match package version v{version}")
    return version


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_release(
    root: Path, artifacts: Path, output: Path, tag: str, commit: str
) -> dict[str, object]:
    version = check_version(root, tag)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Release commit must be a full lowercase Git SHA")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Release output directory must be empty")

    selected = []
    for platform, architecture, extension, template in ASSETS:
        artifact = f"rati-swarm-{platform}"
        matches = sorted((artifacts / artifact).rglob(f"*.{extension}"))
        expected = template.format(version=version)
        if len(matches) != 1:
            raise ValueError(
                f"Expected one {extension} installer in {artifact}, got {len(matches)}"
            )
        source = matches[0]
        if source.name != expected:
            raise ValueError(f"Expected installer {expected!r}, got {source.name!r}")
        if source.is_symlink() or not source.is_file() or source.stat().st_size == 0:
            raise ValueError(f"Installer must be a nonempty regular file: {source}")
        name = f"RATi-Runners-{version}-{platform}-{architecture}.{extension}"
        selected.append((source, name, platform, architecture))

    output.mkdir(parents=True, exist_ok=True)
    assets = []
    for source, name, platform, architecture in selected:
        destination = output / name
        shutil.copyfile(source, destination)
        assets.append(
            {
                "name": name,
                "platform": platform,
                "architecture": architecture,
                "source": source.relative_to(artifacts).as_posix(),
                "size": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
        )
    manifest = {
        "version": version,
        "tag": tag,
        "commit": commit,
        "assets": sorted(assets, key=lambda asset: asset["name"]),
    }
    (output / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checksums = "".join(
        f"{_sha256(path)}  {path.name}\n" for path in sorted(output.iterdir())
    )
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Check and prepare RATi desktop release assets.")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check-version")
    check.add_argument("--tag")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--tag", required=True)
    prepare.add_argument("--commit", required=True)
    prepare.add_argument("--artifacts", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        if arguments.command == "check-version":
            print(check_version(ROOT, arguments.tag))
        else:
            manifest = prepare_release(
                ROOT, arguments.artifacts, arguments.output, arguments.tag, arguments.commit
            )
            print(f"Prepared {len(manifest['assets'])} installers for {arguments.tag}")
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Release preparation failed: {error}\n")


if __name__ == "__main__":
    main()
