from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


class ResearchRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=6_000)
    symbols: list[str] = Field(default_factory=list, max_length=20)
    model: str | None = Field(default=None, max_length=160)


ResearchTransport = Callable[[str, dict[str, Any]], dict[str, Any]]


def _request_openrouter(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv("RATI_NODE_PUBLIC_ORIGIN", "https://rati.chat"),
            "X-Title": "RATi Desktop",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise RuntimeError(f"OpenRouter research failed ({exc.code}): {detail}") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError("OpenRouter research could not be completed") from exc


class OpenRouterResearch:
    """Small standalone research client backed by the node-owned credential."""

    def __init__(self, transport: ResearchTransport = _request_openrouter) -> None:
        self.transport = transport

    def run(self, request: ResearchRequest, api_key: str) -> dict[str, Any]:
        model = request.model or os.getenv("OPENROUTER_MODEL", "openrouter/auto")
        symbols = [symbol.strip().upper() for symbol in request.symbols if symbol.strip()]
        context = f" Symbols: {', '.join(symbols)}." if symbols else ""
        result = self.transport(
            api_key,
            {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are the optional research layer for RATi. Separate observations "
                            "from inference, state uncertainty, never invent live prices, and do "
                            "not present the answer as personalized financial advice."
                        ),
                    },
                    {"role": "user", "content": request.prompt.strip() + context},
                ],
            },
        )
        choices = result.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("OpenRouter returned no research answer")
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("OpenRouter returned an empty research answer")
        return {
            "status": "complete",
            "provider": "openrouter",
            "model": str(result.get("model") or model),
            "answer": content.strip(),
            "symbols": symbols,
            "generated_at": datetime.now(UTC).isoformat(),
            "usage": result.get("usage") if isinstance(result.get("usage"), dict) else None,
        }
