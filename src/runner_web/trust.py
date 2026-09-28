"""trust.rati.chat: the RATi Rules' public record.

The rules live in the private ratitrust repository. This page publishes, for
the current version, its number, date and digest (a commitment to the text),
and every revealed version in full, with its digest recomputed here from the
files so a reader can see it matches. From 1.0.1 the rules are data
(rules.toml, with a JSON Schema), shown here as structured standards; 1.0.0
was Markdown and is shown as its text.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path
from typing import Any

ASSETS = Path(__file__).parent / "assets"
RULES_FILE = "src/ratitrust/rules.toml"
SCHEMA_FILE = "src/ratitrust/rules.schema.json"
OPERATORS = {
    "==": "is",
    ">=": "at least",
    "<=": "at most",
    "in": "one of",
    "none_of": "none of",
    "absent": "none",
    "none_within": "none in the last",
    "between": "between",
}


def _load(name: str) -> dict[str, Any] | None:
    try:
        return json.loads((ASSETS / name).read_text())
    except (OSError, ValueError):
        return None


def recomputed_digest(record: dict[str, Any]) -> str:
    """The digest over a revealed record's files, as ratitrust computes it."""

    files = record.get("files") or {}
    manifest = "".join(
        f"{path} {hashlib.sha256(files[path].encode()).hexdigest()}\n" for path in sorted(files)
    )
    return hashlib.sha256(manifest.encode()).hexdigest()


def rules_record() -> dict[str, Any]:
    """The current commitment and every revealed version, newest first."""

    current = _load("trust-rules-current.json")
    revealed = []
    for path in sorted(ASSETS.glob("trust-rules-*.json")):
        if path.name == "trust-rules-current.json":
            continue
        record = _load(path.name)
        if not record or not record.get("files"):
            continue
        record["verified"] = recomputed_digest(record) == record.get("digest")
        record["rules"] = parsed_rules(record["files"].get(RULES_FILE))
        record["rules_text"] = record["files"].get("RULES.md", "")
        record["methodology_text"] = record["files"].get("METHODOLOGY.md", "")
        revealed.append(record)
    revealed.sort(key=lambda record: [int(part) for part in record["version"].split(".")])
    revealed.reverse()
    # The rules shown are the current version's when it is revealed, else the
    # newest revealed one, which the page says is not the version in force.
    shown = next(
        (record for record in revealed if current and record["version"] == current["version"]),
        None,
    )
    sealed = shown is None
    if shown is None:
        shown = next((record for record in revealed if record.get("rules")), None)
    return {"current": current, "revealed": revealed, "shown": shown, "sealed": sealed}


def parsed_rules(text: str | None) -> dict[str, Any] | None:
    """A version's rules.toml, with each test written out for reading."""

    if not text:
        return None
    try:
        rules = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    for asset in (rules.get("assets") or {}).values():
        facts = {fact["id"]: fact for fact in asset.get("facts") or []}
        lists = asset.get("lists") or {}
        used: set[str] = set()
        for standard in asset.get("standards") or []:
            clauses = [*standard["test"], standard.get("not_applied") or {}]
            standard["tests"] = [describe_clause(clause, lists) for clause in standard["test"]]
            if standard.get("not_applied"):
                standard["not_applied_text"] = describe_clause(standard["not_applied"], lists)
            standard["sources"] = [facts[name] for name in standard["facts"] if name in facts]
            standard["lists_used"] = {
                clause["list"]: lists[clause["list"]]
                for clause in clauses
                if clause.get("list") in lists
            }
            used |= set(standard["lists_used"])
        # Lists no test names directly, such as the pools left out of the top holders.
        asset["other_lists"] = {name: items for name, items in lists.items() if name not in used}
    return rules


def describe_clause(clause: dict[str, Any], lists: dict[str, Any]) -> str:
    """One test clause in words, e.g. "real_liquidity at least 10,000 USD"."""

    op = OPERATORS.get(clause["op"], clause["op"])
    value = clause.get("value")
    if "list" in clause:
        items = lists.get(clause["list"]) or []
        target = f"{clause['list']} ({len(items)})"
    elif isinstance(value, list):
        target = " and ".join(_number(item) for item in value)
    elif value is None:
        target = ""
    else:
        target = _number(value)
    unit = f" {clause['unit']}" if clause.get("unit") else ""
    text = f"{clause['fact']} {op} {target}{unit}".strip()
    if clause.get("unless"):
        text += f", unless {describe_clause(clause['unless'], lists)}"
    if clause.get("when"):
        text += f", when {describe_clause(clause['when'], lists)}"
    return text


def _number(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, int | float):
        return str(value)
    # Group thousands in amounts, not in codes such as a four-digit SIC.
    grouping = "," if abs(value) >= 10_000 else ""
    return f"{value:{grouping}.0f}" if float(value).is_integer() else f"{value:{grouping}}"


def published_file(name: str) -> str | None:
    """The newest revealed version's rules.toml or schema, as published."""

    path = {"rules.toml": RULES_FILE, "rules.schema.json": SCHEMA_FILE}.get(name)
    for record in rules_record()["revealed"]:
        if path and path in record["files"] and record["verified"]:
            return record["files"][path]
    return None


# Requests made before the form was removed stay readable to operations.
def access_requests(database: Any, limit: int = 200) -> list[dict[str, Any]]:
    rows = database.execute(
        "SELECT github_login,github_id,reason,status,checks_json,requested_at "
        "FROM trust_access_requests ORDER BY requested_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{**dict(row), "checks": json.loads(row["checks_json"] or "{}")} for row in rows]
