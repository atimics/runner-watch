from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from runner_web import telegram


class _FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"ok":true}') -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def test_escape_markdown_v2_escapes_each_special_character() -> None:
    for special in "_*[]()~`" + chr(92):
        assert ("\\" + special) == telegram.escape_markdown_v2(special)
    # Line-context specials escape only at line-start or after whitespace, which
    # is the placement rule Telegram parses. Stretch the sample so each one is in
    # that position and confirm the escape.
    samples = {
        ">": " >x",
        "#": " #x",
        "+": " +x",
        "-": " -x",
        "=": " =x",
        "|": " |x",
        "{": " {x",
        "}": " }x",
        ".": " .x",
        "!": " !x",
    }
    for special, where in samples.items():
        out = telegram.escape_markdown_v2(where)
        assert "\\" + special in out, (special, out)


def test_first_url_picks_the_ticker_link_for_preview() -> None:
    message = (
        "Runners alert\n\n"
        "\u26A1 *SOUN*\n\n"
        "$8.42\n"
        "https://runners.rati.chat/t/SOUN\n\n"
        "https://runners.rati.chat/research/some-id\n"
    )
    assert telegram._first_url(message) == "https://runners.rati.chat/t/SOUN"


@pytest.fixture
def patch_urlopen(monkeypatch: pytest.MonkeyPatch):
    import urllib.request as _real_urllib

    opened: list[urllib.request.Request] = []
    bodies: list[tuple[dict[str, Any], str]] = []

    def _open(request: urllib.request.Request, timeout: int = 0) -> _FakeResponse:
        import json

        opened.append(request)
        body = json.dumps(
            bodies[-1][0] if bodies else {"ok": True}
        ).encode()
        return _FakeResponse(200, body=body)

    monkeypatch.setattr(_real_urllib, "urlopen", _open)
    return opened, bodies


def _config() -> telegram.TelegramConfig:
    return telegram.TelegramConfig(bot_token="tkn", chat_id="123")


def test_send_post_emits_parse_mode_and_preview_options(patch_urlopen) -> None:
    opened, _ = patch_urlopen
    telegram.send_post(_config(), "*bold* https://runners.rati.chat/t/SOUN")
    payload = _url_payload(opened[0])
    assert payload["parse_mode"] == "MarkdownV2"
    assert payload["text"].startswith("*bold* https://runners.rati.chat/t/SOUN")
    assert payload["disable_notification"] is False
    assert payload["link_preview_options"]["is_disabled"] is False


def test_send_post_omits_preview_options_when_body_has_no_url(patch_urlopen) -> None:
    """Text without a URL has nothing to preview; the Telegram call stays clean."""

    opened, _ = patch_urlopen
    telegram.send_post(_config(), "*bold* only")
    payload = _url_payload(opened[0])
    assert "link_preview_options" not in payload
    assert payload["text"] == "*bold* only"


def test_send_post_anchors_preview_when_url_is_passed(patch_urlopen) -> None:
    opened, _ = patch_urlopen
    telegram.send_post(
        _config(),
        "\U0001F7E2 *1 new on the board*\n\nRunners.",
        preview_url="https://runners.rati.chat/t/SOUN",
    )
    payload = _url_payload(opened[0])
    assert payload["text"].startswith("https://runners.rati.chat/t/SOUN\n\n")
    assert payload["link_preview_options"]["url"] == "https://runners.rati.chat/t/SOUN"


def test_send_post_falls_back_to_plain_on_parse_error(
    patch_urlopen, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened, bodies = patch_urlopen

    def _open(
        request: urllib.request.Request, timeout: int = 0
    ) -> _FakeResponse:
        opened.append(request)
        # First call returns a parse error so we fall back.
        if len(opened) == 1:
            return _FakeResponse(
                400,
                body=b'{"ok":false,"description":"Bad request: could not parse entities"}',
            )
        return _FakeResponse(200, body=b'{"ok":true}')

    import urllib.request as _real_urllib

    monkeypatch.setattr(_real_urllib, "urlopen", _open)
    telegram.send_post(_config(), "hello *unbalanced")
    assert len(opened) >= 2
    fallback = _url_payload(opened[1])
    assert "parse_mode" not in fallback
    # The body had no URL, so the fallback stays free of link previews.
    assert "link_preview_options" not in fallback


def test_send_post_raises_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = telegram.TelegramConfig(bot_token="", chat_id="")
    with pytest.raises(RuntimeError, match="bot token and chat id"):
        telegram.send_post(config, "hi")


def _url_payload(request: urllib.request.Request) -> dict[str, Any]:
    import json

    body = request.data.decode("utf-8") if isinstance(request.data, bytes) else request.data
    return json.loads(body)


def test_batched_update_renders_cheetah_state_price_and_link() -> None:
    message = telegram.format_update_announcement_md(
        {
            "runners": [
                {
                    "ticker": "SOUN",
                    "tag": "RUNNING",
                    "price": 8.42,
                    "change_pct": 18.3,
                    "relative_volume": 3.4,
                    "score": 88,
                }
            ],
            "reports": [],
            "events": [],
        },
        origin="https://runners.rati.chat",
    )
    lines = message.split("\n")
    assert lines[0] == "\U0001F406 *1 new on the board*"
    assert "\u26A1 *SOUN*  \u2014  RUNNING" in message, message
    assert "$8.42" in message, message
    assert "*+18.3%" in message and "*3.4\u00D7*" in message and "score *88*" in message
    assert message.endswith("https://runners.rati.chat/t/SOUN"), message


def test_batched_update_counts_and_links_every_runner() -> None:
    two = telegram.format_update_announcement_md(
        {
            "runners": [
                {"ticker": "A", "tag": "RUNNING", "score": 88},
                {"ticker": "B", "tag": "SETUP", "score": 60},
            ],
            "reports": [],
            "events": [],
        },
        origin="http://app.test",
    )
    assert "2 new on the board" in two
    assert "/t/B" in two and "/t/A" in two


def test_market_report_link_falls_back_to_base_when_day_unknown() -> None:
    payload = {
        "report_type": "pre_market",
        "label": "Pre-market briefing",
        "headline": "x",
        "summary": "y",
        "leaders": [],
        "report_day": "",
    }
    message = telegram.format_market_report_post_md(
        payload, origin="http://app.test"
    )
    # Exact block comparison; an endswith check on a bare origin trips
    # CodeQL's incomplete URL substring sanitization rule.
    assert message.split("\n\n")[-1] == "http://app.test"


def test_public_report_includes_research_link_when_public_id_present() -> None:
    message = telegram.format_public_report_post_md(
        {"ticker": "CAST", "public_id": "pub-one", "headline": "report"},
        origin="http://app.test",
    )
    assert "http://app.test/t/CAST" in message
    # Hyphens in URLs are MDV2 escaped to keep escaping safe across the message.
    assert "http://app.test/research/pub" in message


def test_event_post_lists_origin_and_source() -> None:
    message = telegram.format_event_post_md(
        {"ticker": "SOUN", "kind": "8-K", "headline": "Item 5.02", "age": "4m", "source": "sec"},
        origin="http://app.test",
    )
    assert "\U0001F4F0 *Event on $SOUN*" in message
    assert "filed via SEC" in message
    assert message.endswith("http://app.test/t/SOUN"), message


def test_release_announcement_returns_empty_without_notes() -> None:
    assert telegram.format_release_announcement_md("1.2.3", "   ", origin="x") == ""


def test_release_announcement_leads_with_the_cheetah() -> None:
    message = telegram.format_release_announcement_md(
        "1.0.1", "Calls page ships.", origin="https://runners.rati.chat"
    )
    assert message.startswith("\U0001F406 *RATi Runners 1.0.1*"), message
