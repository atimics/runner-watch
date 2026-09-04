from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BINARY = ROOT / "desktop" / ".scanner-dist" / (
    "rati-scanner.exe" if sys.platform == "win32" else "rati-scanner"
)


def _lines(stream: object, output: queue.Queue[str]) -> None:
    for line in stream:
        output.put(str(line).strip())


def _capture(stream: object, output: list[str]) -> None:
    for line in stream:
        output.append(str(line).strip())
        del output[:-100]


def _get(url: str, token: str = "") -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        return json.loads(response.read().decode())


def main() -> None:
    parser = argparse.ArgumentParser(description="Start and verify a packaged RATi scanner.")
    parser.add_argument("binary", nargs="?", type=Path, default=DEFAULT_BINARY)
    arguments = parser.parse_args()
    binary = arguments.binary.resolve()
    if not binary.is_file():
        raise SystemExit(f"Scanner binary was not found at {binary}")

    token = "ci-scanner-token-with-at-least-24-characters"
    with tempfile.TemporaryDirectory(
        prefix="rati-scanner-smoke-",
        ignore_cleanup_errors=sys.platform == "win32",
    ) as directory:
        environment = {
            **os.environ,
            "DATABASE_PATH": str(Path(directory) / "receipts.sqlite3"),
            "RATI_CREDENTIAL_BACKEND": "memory",
            "RATI_NODE_HOST": "127.0.0.1",
            "RATI_NODE_MODE": "local",
            "RATI_NODE_PORT": "0",
            "RATI_NODE_TOKEN": token,
        }
        process = subprocess.Popen(
            [str(binary)],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        output: queue.Queue[str] = queue.Queue()
        assert process.stdout is not None
        threading.Thread(target=_lines, args=(process.stdout, output), daemon=True).start()
        errors: list[str] = []
        assert process.stderr is not None
        threading.Thread(
            target=_capture,
            args=(process.stderr, errors),
            daemon=True,
        ).start()
        try:
            deadline = time.monotonic() + 90
            scanner_url = ""
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    line = output.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "ready":
                    scanner_url = str(event.get("url") or "")
                    break
            if not scanner_url:
                detail = "\n".join(errors[-20:]) or "No stderr output"
                raise SystemExit(
                    "Scanner did not announce readiness "
                    f"(exit={process.poll()})\n{detail}"
                )

            while time.monotonic() < deadline:
                try:
                    node = _get(f"{scanner_url}/api/v1/node")
                    receipts = _get(f"{scanner_url}/api/v1/scans", token)
                    if node.get("api_version") == "1" and receipts.get("receipts") == []:
                        print(f"Packaged scanner is healthy at {scanner_url}")
                        return
                except (OSError, ValueError, urllib.error.URLError):
                    time.sleep(0.25)
            raise SystemExit("Scanner API did not become healthy before the timeout")
        finally:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
