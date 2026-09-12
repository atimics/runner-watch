"""AI-generated portraits for market actors, served from a cache.

The portrait is a fictional mascot, never a real likeness. It is generated once
per actor through OpenRouter's image models and then stored, so the same
character keeps the same face and the app never pays twice. If generation is
unavailable or over budget the actor keeps the procedural face and the map
still renders.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from runner_web.db import connection
from runner_web.pseudonyms import comment_avatar_ability

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_PORTRAIT_MODEL = "google/gemini-3.1-flash-image"
PORTRAIT_MODEL = os.getenv("ACTOR_PORTRAIT_MODEL", DEFAULT_PORTRAIT_MODEL)
DAILY_LIMIT = max(0, int(os.getenv("ACTOR_PORTRAIT_DAILY_LIMIT", "200")))
TIMEOUT_SECONDS = max(20, int(os.getenv("ACTOR_PORTRAIT_TIMEOUT_SECONDS", "120")))
MAX_PORTRAIT_BYTES = 600_000

PortraitTransport = Callable[[str, dict[str, Any]], dict[str, Any]]

_PROMPT = (
    "Create a square avatar portrait for a fictional market-analysis character. "
    "Show one original mascot creature or abstract being, not a human, friendly "
    "but sharp, head and shoulders, bold flat colors, simple uncluttered "
    "background. {ability} Do not depict or resemble any real person, celebrity, "
    "or public figure. No text, no letters, no numbers, no logos, no flags, no "
    "watermarks."
)


def _iso(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).isoformat()


def _day(at: datetime | None = None) -> str:
    return (at or datetime.now(UTC)).date().isoformat()


def _request_openrouter(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": "Runner Watch",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode())


def _daily_count(db: Any, day: str) -> int:
    row = db.execute(
        "SELECT value FROM worker_state WHERE key=?", (f"actor_portraits:{day}",)
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        return 0


def _bump_daily_count(db: Any, day: str, *, at: datetime | None = None) -> None:
    count = _daily_count(db, day) + 1
    db.execute(
        """
        INSERT INTO worker_state(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
        """,
        (f"actor_portraits:{day}", str(count), _iso(at)),
    )


def _image_from_result(result: dict[str, Any]) -> tuple[bytes, str] | None:
    choices = result.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message") or {}
    candidates: list[Any] = []
    for image in message.get("images") or []:
        candidates.append(image)
    content = message.get("content")
    if isinstance(content, list):
        candidates.extend(content)
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        image_url = candidate.get("image_url")
        if isinstance(image_url, dict):
            url = image_url.get("url")
        else:
            url = candidate.get("url")
        if not isinstance(url, str) or not url.startswith("data:"):
            continue
        header, _, data = url.partition(",")
        if not data:
            continue
        content_type = "image/png"
        if header.startswith("data:") and ";" in header:
            content_type = header[5:].split(";", 1)[0] or content_type
        try:
            binary = base64.b64decode(data, validate=True)
        except (ValueError, TypeError):
            continue
        if 0 < len(binary) <= MAX_PORTRAIT_BYTES:
            return binary, content_type
    return None


def portrait_for_actor(actor_id: str) -> dict[str, Any] | None:
    with connection() as db:
        row = db.execute(
            "SELECT content_type,bytes FROM market_actor_portraits WHERE actor_id=?",
            (actor_id,),
        ).fetchone()
    if row is None:
        return None
    return {"content_type": str(row["content_type"]), "bytes": bytes(row["bytes"])}


def _mark_status(actor_id: str, status: str) -> None:
    with connection() as db:
        db.execute(
            "UPDATE market_actors SET portrait_status=?,updated_at=? WHERE id=?",
            (status, _iso(), actor_id),
        )


def generate_actor_portrait(
    actor_id: str,
    *,
    api_key: str,
    transport: PortraitTransport | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Generate and cache one portrait. Never raises."""

    existing = portrait_for_actor(actor_id)
    if existing is not None:
        return {"status": "ready"}
    if not api_key:
        _mark_status(actor_id, "fallback")
        return {"status": "fallback", "reason": "no_key"}
    day = _day(at)
    with connection() as db:
        actor = db.execute(
            "SELECT display_name,ability_id,portrait_status FROM market_actors WHERE id=?",
            (actor_id,),
        ).fetchone()
        if actor is None:
            return {"status": "missing"}
        count = _daily_count(db, day)
    if DAILY_LIMIT <= 0 or count >= DAILY_LIMIT:
        _mark_status(actor_id, "fallback")
        return {"status": "fallback", "reason": "budget"}
    ability = comment_avatar_ability(str(actor["ability_id"]))
    payload = {
        "model": PORTRAIT_MODEL,
        "modalities": ["image", "text"],
        "messages": [
            {
                "role": "user",
                "content": _PROMPT.format(ability=str(ability["description"])),
            }
        ],
        "max_tokens": 64,
    }
    call = transport or _request_openrouter
    try:
        result = call(api_key, payload)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        _mark_status(actor_id, "fallback")
        return {"status": "fallback", "reason": "transport", "error": str(exc)[:200]}
    image = _image_from_result(result if isinstance(result, dict) else {})
    if image is None:
        _mark_status(actor_id, "fallback")
        return {"status": "fallback", "reason": "no_image"}
    binary, content_type = image
    timestamp = _iso(at)
    with connection() as db:
        db.execute(
            """
            INSERT INTO market_actor_portraits(actor_id,content_type,bytes,created_at)
            VALUES(?,?,?,?) ON CONFLICT(actor_id) DO UPDATE SET
                content_type=excluded.content_type,bytes=excluded.bytes,
                created_at=excluded.created_at
            """,
            (actor_id, content_type, binary, timestamp),
        )
        db.execute(
            "UPDATE market_actors SET portrait_status='ready',updated_at=? WHERE id=?",
            (timestamp, actor_id),
        )
        _bump_daily_count(db, day, at=at)
    return {"status": "ready"}
