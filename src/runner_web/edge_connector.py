from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from runner_web.llm_routing import LLMRouteError, call_chat_completions


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _cloud_origin(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("RATi origin is invalid")
    is_local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
        raise ValueError("RATi origin must use HTTPS")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def _cloud_json(
    origin: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{origin}{path}",
        data=json.dumps(payload or {}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("RATi returned an invalid connector response")
    return result


def run_once(
    *,
    origin: str,
    token: str,
    local_base_url: str,
    local_api_key: str | None = None,
    model_override: str | None = None,
) -> bool:
    claimed = _cloud_json(origin, "/api/llm/edge/jobs/claim", token)
    job = claimed.get("job")
    if not isinstance(job, dict):
        return False
    job_id = str(job.get("id") or "")
    request_body = job.get("request")
    if not job_id or not isinstance(request_body, dict):
        raise ValueError("RATi returned an invalid local model job")
    request_body = dict(request_body)
    request_body["model"] = model_override or str(job.get("model") or request_body.get("model"))
    try:
        result = call_chat_completions(
            local_base_url,
            request_body,
            api_key=local_api_key,
            allow_local=True,
            timeout=300,
        )
    except (LLMRouteError, ValueError) as exc:
        message = str(exc)[:500] or "The local model request failed."
        _cloud_json(origin, f"/api/llm/edge/jobs/{job_id}/fail", token, {"error": message})
        return True
    _cloud_json(
        origin,
        f"/api/llm/edge/jobs/{job_id}/complete",
        token,
        {"response": result},
        timeout=60,
    )
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Connect a local OpenAI-compatible model to RATi.",
    )
    parser.add_argument(
        "--rati-origin",
        default=os.getenv("RATI_ORIGIN", "https://runners.rati.chat"),
        help="RATi site origin. Defaults to RATI_ORIGIN.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("RATI_EDGE_TOKEN", ""),
        help="One-time connector token from model settings. Defaults to RATI_EDGE_TOKEN.",
    )
    parser.add_argument(
        "--local-base-url",
        default=os.getenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
        help="Local OpenAI-compatible /v1 URL.",
    )
    parser.add_argument(
        "--local-api-key",
        default=os.getenv("LOCAL_LLM_API_KEY", ""),
        help="Optional local API key. It is never sent to RATi.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LOCAL_LLM_MODEL", ""),
        help="Optional local model ID override.",
    )
    parser.add_argument("--once", action="store_true", help="Handle at most one job, then exit.")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.token:
        raise SystemExit("Set RATI_EDGE_TOKEN or pass --token.")
    try:
        origin = _cloud_origin(args.rati_origin)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"RATi local model connector is polling {origin}.", flush=True)
    while True:
        try:
            handled = run_once(
                origin=origin,
                token=args.token,
                local_base_url=args.local_base_url,
                local_api_key=args.local_api_key or None,
                model_override=args.model or None,
            )
        except KeyboardInterrupt:
            print("Connector stopped.", flush=True)
            return
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            print(f"Connector error: {exc}", file=sys.stderr, flush=True)
            handled = False
        if args.once:
            return
        if not handled:
            time.sleep(max(0.5, args.poll_seconds))


if __name__ == "__main__":
    main()
