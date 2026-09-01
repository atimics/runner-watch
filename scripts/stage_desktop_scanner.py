from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "rati-scanner.exe" if sys.platform == "win32" else "rati-scanner"
SOURCE = ROOT / "desktop" / ".scanner-dist" / SOURCE_NAME


def rust_host_tuple() -> str:
    completed = subprocess.run(  # noqa: S603
        ["rustc", "--print", "host-tuple"],
        check=True,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if not value or any(character.isspace() for character in value):
        raise SystemExit("rustc returned an invalid host tuple")
    return value


def destination() -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    return (
        ROOT
        / "desktop"
        / "src-tauri"
        / "binaries"
        / f"rati-scanner-{rust_host_tuple()}{suffix}"
    )


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Scanner build was not found at {SOURCE}")
    target = destination()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, target)
    if sys.platform != "win32":
        target.chmod(0o755)
    print(f"Staged {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
