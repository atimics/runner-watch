"""Record the replay checks from CI's actual JUnit reports and reviewed sources."""

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from runner_web.memecoin_replay import POLICY, digest

parser = argparse.ArgumentParser()
parser.add_argument("--reports", nargs="+", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
modules = {
    "tests.test_memecoin_replay",
    "tests.test_memecoin_replay_telegram",
    "tests.test_browser_memecoin_replay",
}
outcomes = {}
seen = set()
errors = []
for path in args.reports:
    try:
        report_xml = ElementTree.parse(path)
    except (OSError, ElementTree.ParseError):
        errors.append("Report unavailable: " + Path(path).name)
        continue
    for test in report_xml.iter("testcase"):
        module = test.get("classname", "")
        if module not in modules:
            continue
        seen.add(module)
        result = "passed"
        for tag in ("failure", "error", "skipped"):
            if test.find(tag) is not None:
                result = tag
        outcomes[module + "." + test.get("name", "")] = result
sources = [
    *Path("src/runner_web").glob("memecoin_replay*.py"),
    Path("src/runner_web/memecoin_chain_parser.py"),
    Path("src/runner_web/telegram.py"),
    Path("src/runner_web/db.py"),
    Path("src/runner_web/main.py"),
    Path("src/runner_web/operations.py"),
    Path("fly.toml"),
    Path("src/runner_web/memecoin_store.py"),
    Path("web/static/memecoin-replay.js"),
    Path("web/static/memecoin-replay.css"),
    Path("web/templates/simple_coin_detail.html"),
    Path("web/templates/market_screen.html"),
    Path("web/templates/_memecoin_replay.html"),
    Path(__file__).resolve().relative_to(Path.cwd()),
    *Path("tests").glob("test*memecoin_replay*.py"),
]
hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(sources)}
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
report = {
    "policy": POLICY,
    "reviewed_head": os.getenv("REPLAY_REVIEWED_HEAD") or commit,
    "tested_commit": commit,
    "source_hashes": hashes,
    "source_digest": digest(hashes),
    "outcomes": outcomes,
    "tests_run": len(outcomes),
    "errors": errors,
    "passed": not errors
    and seen == modules
    and len(outcomes) >= 45
    and set(outcomes.values()) == {"passed"},
}
Path(args.output).write_text(json.dumps(report, indent=2) + "\n")
print(
    json.dumps(
        {
            "passed": report["passed"],
            "tests_run": len(outcomes),
            "reviewed_head": report["reviewed_head"],
        }
    )
)
raise SystemExit(0 if report["passed"] else 1)
