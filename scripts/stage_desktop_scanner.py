from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_NAME = "rati-scanner.exe" if sys.platform == "win32" else "rati-scanner"
SOURCE = ROOT / "desktop" / ".scanner-dist" / SOURCE_NAME
DESTINATION = ROOT / "desktop" / "resources" / "scanner" / SOURCE_NAME


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"Scanner build was not found at {SOURCE}")
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SOURCE, DESTINATION)
    if sys.platform != "win32":
        DESTINATION.chmod(0o755)
    print(f"Staged {DESTINATION.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
