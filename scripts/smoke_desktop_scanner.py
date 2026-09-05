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
DEFAULT_BINARY = (
    ROOT
    / "desktop"
    / ".scanner-dist"
    / ("rati-scanner.exe" if sys.platform == "win32" else "rati-scanner")
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


def _check_desktop_cors(url: str, token: str) -> None:
    for origin in ("tauri://localhost", "http://tauri.localhost"):
        request = urllib.request.Request(
            f"{url}/api/v1/scans",
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.headers.get("Access-Control-Allow-Origin") != origin:
                raise SystemExit(f"Packaged scanner needs CORS access for {origin}")
        request = urllib.request.Request(
            f"{url}/api/v1/scans",
            headers={"Origin": origin, "Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.headers.get("Access-Control-Allow-Origin") != origin:
                raise SystemExit(f"Packaged scanner needs authenticated CORS access for {origin}")


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
            "RATI_NODE_EXIT_ON_STDIN_CLOSE": "1",
            "RATI_NODE_PORT": "0",
            "RATI_NODE_TOKEN": token,
        }
        process = subprocess.Popen(
            [str(binary)],
            env=environment,
            stdin=subprocess.PIPE,
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
                    f"Scanner did not announce readiness (exit={process.poll()})\n{detail}"
                )

            while time.monotonic() < deadline:
                try:
                    node = _get(f"{scanner_url}/api/v1/node")
                    receipts = _get(f"{scanner_url}/api/v1/scans", token)
                    if node.get("api_version") == "1" and receipts.get("receipts") == []:
                        _check_desktop_cors(scanner_url, token)
                        assert process.stdin is not None
                        process.stdin.close()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired as exc:
                            raise SystemExit("Scanner stayed alive after desktop shutdown") from exc
                        try:
                            _get(f"{scanner_url}/api/v1/node")
                        except OSError:
                            pass
                        else:
                            raise SystemExit("Scanner API stayed open after desktop shutdown")
                        print("Packaged scanner passed startup, desktop CORS, and shutdown checks")
                        return
                except (OSError, ValueError, urllib.error.URLError):
                    time.sleep(0.25)
            raise SystemExit("Scanner API did not become healthy before the timeout")
        finally:
            if process.poll() is None and sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                )
            elif process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
