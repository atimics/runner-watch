"""trust.rati.chat: the RATi Rules' public record and access requests.

The rules live in the private ratitrust repository. This page publishes, for
the current version, its number, date and digest (a commitment to the text),
and every revealed version in full, with its digest recomputed here from the
files so a reader can see it matches. From 1.0.1 the rules are data
(rules.toml, with a JSON Schema), shown here as structured standards; 1.0.0
was Markdown and is shown as its text.

Access to the repository is by request with a GitHub account that looks real:
it exists, is a person's account, is at least 30 days old and has some public
activity. That shows the account is genuine, not that the requester owns it;
access is only ever granted by invitation to that account.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
GITHUB_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
MIN_ACCOUNT_AGE = timedelta(days=30)
MAX_REASON = 500
# The page shows these by code, so a link cannot put arbitrary words on it.
MESSAGES = {
    "invalid": "That is not a GitHub username.",
    "unreachable": "GitHub could not be reached. Try again shortly.",
    "missing": "No GitHub account has that name.",
    "organisation": "Request with a person's account, not an organisation.",
    "undated": "GitHub did not say when that account was made.",
    "young": "That account is under 30 days old.",
    "inactive": "That account has no public repositories or followers.",
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
    return text


def _number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
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


def fetch_github_user(login: str) -> dict[str, Any] | None:
    request = urllib.request.Request(
        f"https://api.github.com/users/{login}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "RATi-trust/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read(256 * 1024))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def check_github_account(
    login: str,
    *,
    fetch: Callable[[str], dict[str, Any] | None] | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Whether a GitHub account looks real enough to invite, and why not."""

    login = login.strip().lstrip("@")
    if not GITHUB_LOGIN.fullmatch(login):
        return {"ok": False, "code": "invalid"}
    try:
        user = (fetch or fetch_github_user)(login)
    except Exception:
        return {"ok": False, "code": "unreachable"}
    if not user:
        return {"ok": False, "code": "missing"}
    if user.get("type") != "User":
        return {"ok": False, "code": "organisation"}
    try:
        created = datetime.fromisoformat(str(user["created_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return {"ok": False, "code": "undated"}
    current = at or datetime.now(UTC)
    if current - created < MIN_ACCOUNT_AGE:
        return {"ok": False, "code": "young"}
    if not (user.get("public_repos") or user.get("followers")):
        return {"ok": False, "code": "inactive"}
    return {
        "ok": True,
        "login": str(user.get("login") or login),
        "github_id": int(user["id"]),
        "checks": {
            "created_at": created.isoformat(),
            "public_repos": int(user.get("public_repos") or 0),
            "followers": int(user.get("followers") or 0),
        },
    }


def record_access_request(
    database: Any,
    account: dict[str, Any],
    *,
    reason: str,
    ip_hash: str,
    at: datetime,
) -> bool:
    """Save a request; False when this account already has one waiting."""

    waiting = database.execute(
        "SELECT 1 FROM trust_access_requests WHERE github_id=? AND status='pending'",
        (account["github_id"],),
    ).fetchone()
    if waiting:
        return False
    database.execute(
        """
        INSERT INTO trust_access_requests(
            github_login,github_id,reason,status,checks_json,ip_hash,requested_at
        ) VALUES(?,?,?,'pending',?,?,?)
        """,
        (
            account["login"],
            account["github_id"],
            reason.strip()[:MAX_REASON],
            json.dumps(account["checks"]),
            ip_hash,
            at.isoformat(),
        ),
    )
    return True


def access_requests(database: Any, limit: int = 200) -> list[dict[str, Any]]:
    rows = database.execute(
        "SELECT github_login,github_id,reason,status,checks_json,requested_at "
        "FROM trust_access_requests ORDER BY requested_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{**dict(row), "checks": json.loads(row["checks_json"] or "{}")} for row in rows]
