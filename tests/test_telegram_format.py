from __future__ import annotations

import io
import urllib.error
from typing import Any

import pytest

from runner_web import telegram
from runner_web.telegram import strip_markdown_v2


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
        "\u26a1 *SOUN*\n\n"
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
        body = json.dumps(bodies[-1][0] if bodies else {"ok": True}).encode()
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
        "\U0001f7e2 *1 new on the board*\n\nRunners.",
        preview_url="https://runners.rati.chat/t/SOUN",
    )
    payload = _url_payload(opened[0])
    # A bare URL is not valid Markdown V2, so the anchor is an inline link.
    assert payload["text"].startswith(
        "[https://runners\\.rati\\.chat/t/SOUN](https://runners.rati.chat/t/SOUN)\n\n"
    )
    assert payload["link_preview_options"]["url"] == "https://runners.rati.chat/t/SOUN"


def test_send_post_falls_back_to_plain_on_parse_error(
    patch_urlopen, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened, bodies = patch_urlopen

    def _open(request: urllib.request.Request, timeout: int = 0) -> _FakeResponse:
        opened.append(request)
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
    assert "link_preview_options" not in fallback


def test_send_post_falls_back_when_urlopen_raises_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[urllib.request.Request] = []

    def _open(request: urllib.request.Request, timeout: int = 0) -> _FakeResponse:
        opened.append(request)
        if len(opened) == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                400,
                "Bad Request",
                {},
                io.BytesIO(b'{"description":"Bad request: could not parse entities"}'),
            )
        return _FakeResponse(200, body=b'{"ok":true}')

    import urllib.request as _real_urllib

    monkeypatch.setattr(_real_urllib, "urlopen", _open)
    telegram.send_post(_config(), "hello *unbalanced")
    assert len(opened) == 2
    assert "parse_mode" not in _url_payload(opened[1])


def test_send_post_raises_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    config = telegram.TelegramConfig(bot_token="", chat_id="")
    with pytest.raises(RuntimeError, match="bot token and chat id"):
        telegram.send_post(config, "hi")


def _url_payload(request: urllib.request.Request) -> dict[str, Any]:
    import json

    body = request.data.decode("utf-8") if isinstance(request.data, bytes) else request.data
    return json.loads(body)


def test_board_segment_renders_cheetah_state_price_and_link() -> None:
    message = telegram.format_board_segment_md(
        [
            {
                "ticker": "SOUN",
                "tag": "RUNNING",
                "price": 8.42,
                "change_pct": 18.3,
                "relative_volume": 3.4,
                "score": 88,
            }
        ],
        origin="https://runners.rati.chat",
    )
    lines = message.split("\n")
    assert lines[0] == "\U0001f406 *1 new on the board*"
    assert "\u26a1 *SOUN*  \u2014  RUNNING" in message, message
    assert "$8\\.42" in message, message
    assert "*\\+18\\.3%" in message and "*3\\.4\u00d7*" in message and "score *88*" in message
    assert message.endswith("[$SOUN](https://runners.rati.chat/t/SOUN)"), message


def test_the_board_segment_names_every_runner_but_links_only_the_leader() -> None:
    """Telegram previews one URL per message, so a link per runner rendered one
    arbitrary thumbnail and nine dead lines. The batch is named in text and the
    single card belongs to the leader."""

    message = telegram.format_board_segment_md(
        [
            {"ticker": "A", "tag": "RUNNING", "score": 88},
            {"ticker": "B", "tag": "SETUP", "score": 60},
            {"ticker": "C", "tag": "WATCH", "score": 45},
        ],
        origin="http://app.test",
    )
    assert "3 new on the board" in message
    assert "*A*" in message and "*B*" in message and "*C*" in message
    assert message.count("http://app.test") == 1
    assert message.endswith("[$A and the rest of the board](http://app.test/t/A)"), message


def test_the_board_segment_is_empty_without_runners() -> None:
    assert telegram.format_board_segment_md([], origin="http://app.test") == ""
    assert telegram.format_board_segment_md(None, origin="http://app.test") == ""


def test_market_report_link_falls_back_to_base_when_day_unknown() -> None:
    payload = {
        "report_type": "pre_market",
        "label": "Pre-market briefing",
        "headline": "x",
        "summary": "y",
        "leaders": [],
        "report_day": "",
    }
    message = telegram.format_market_report_post_md(payload, origin="http://app.test")
    # Exact block comparison; an endswith check on a bare origin trips
    # CodeQL's incomplete URL substring sanitization rule.
    assert message.split("\n\n")[-1] == "[Open report](http://app.test)"


def test_public_report_links_the_report_page_and_nothing_else() -> None:
    """The report page is the one that carries a card, and the ticker is already
    named in the header, so a second link to /t/ only spent an unpreviewable
    line."""

    message = telegram.format_public_report_post_md(
        {"ticker": "CAST", "public_id": "pub-one", "headline": "report"},
        origin="http://app.test",
    )
    assert "$CAST" in message
    assert message.count("http://app.test") == 1
    assert message.endswith("[Read report](http://app.test/research/pub-one)"), message


def test_public_report_falls_back_to_the_ticker_page_without_a_public_id() -> None:
    message = telegram.format_public_report_post_md(
        {"ticker": "CAST", "public_id": "", "headline": "report"},
        origin="http://app.test",
    )
    assert message.count("http://app.test") == 1
    assert message.endswith("[$CAST](http://app.test/t/CAST)"), message


def test_event_post_lists_origin_and_source() -> None:
    message = telegram.format_event_post_md(
        {"ticker": "SOUN", "kind": "8-K", "headline": "Item 5.02", "age": "4m", "source": "sec"},
        origin="http://app.test",
    )
    assert "\U0001f4f0 *Event on $SOUN*" in message
    assert "filed via SEC" in message
    assert message.endswith("[$SOUN](http://app.test/t/SOUN)"), message


def test_release_announcement_returns_empty_without_notes() -> None:
    assert telegram.format_release_announcement_md("1.2.3", "   ", origin="x") == ""


def test_release_announcement_leads_with_the_cheetah() -> None:
    message = telegram.format_release_announcement_md(
        "1.0.1", "Calls page ships.", origin="https://runners.rati.chat"
    )
    assert message.startswith("\U0001f406 *RATi Runners 1\\.0\\.1*"), message
    assert strip_markdown_v2(message).startswith("\U0001f406 RATi Runners 1.0.1")


# ---------------------------------------------------------------------------
# Markdown V2 parse guard.
#
# The channel spent its life posting raw markup: the formatters emitted
# reserved characters unescaped, Telegram rejected every message, and the
# fallback in send_post resent the source as plain text, so the failure never
# surfaced. These walk the rendered output the way Telegram's parser does and
# fail on any reserved character that is not escaped and not a delimiter.
# ---------------------------------------------------------------------------

_RESERVED = set("_*[]()~`>#+-=|{}.!")
_DELIMITERS = set("_*~`")


def _unparsed(message: str) -> list[tuple[int, str, str]]:
    """Return every reserved character Telegram would reject."""

    found: list[tuple[int, str, str]] = []
    index, size = 0, len(message)
    while index < size:
        char = message[index]
        if char == "\\":
            index += 2
            continue
        if char in _DELIMITERS:
            index += 1
            continue
        if char == "[":  # inline link: [label](target)
            close = index + 1
            while close < size and message[close] != "]":
                close += 2 if message[close] == "\\" else 1
            if close + 1 < size and message[close + 1] == "(":
                end = close + 2
                while end < size and message[end] != ")":
                    end += 2 if message[end] == "\\" else 1
                index = end + 1
                continue
        if char in _RESERVED:
            found.append((index, char, message[max(0, index - 20) : index + 10]))
        index += 1
    return found


def _assert_parses(message: str) -> None:
    assert _unparsed(message) == [], (_unparsed(message)[:3], message)


def test_escape_markdown_v2_escapes_reserved_characters_anywhere_in_the_line() -> None:
    """The bug that made every post raw: Telegram reserves these everywhere,
    not only at the start of a line or after a space."""

    assert telegram.escape_markdown_v2("12.5%") == "12\\.5%"
    assert telegram.escape_markdown_v2("8-K") == "8\\-K"
    assert telegram.escape_markdown_v2("v1.4.2") == "v1\\.4\\.2"
    for special in telegram._MD_V2_SPECIAL:
        assert telegram.escape_markdown_v2("a" + special + "b") == "a\\" + special + "b"


def test_every_rendered_post_parses_as_markdown_v2() -> None:
    origin = "https://runners.rati.chat"
    runner = {
        "ticker": "SOUN",
        "tag": "RUNNING",
        "price": 8.42,
        "change_pct": 18.3,
        "relative_volume": 3.4,
        "score": 88,
    }
    _assert_parses(telegram.format_board_segment_md([runner], origin=origin))
    _assert_parses(
        telegram.format_market_report_post_md(
            {
                "report_type": "pre_market",
                "report_day": "2026-09-15",
                "headline": "Futures firm after a 1.2% overnight bid",
                "summary": "Breadth improved.",
                "leaders": [runner],
            },
            origin=origin,
        )
    )
    _assert_parses(
        telegram.format_public_report_post_md(
            {"ticker": "CAST", "public_id": "pub-one", "headline": "Bookings up 12.5%."},
            origin=origin,
        )
    )
    _assert_parses(
        telegram.format_event_post_md(
            {
                "ticker": "SOUN",
                "kind": "8-K",
                "headline": "Item 5.02",
                "age": "4m",
                "source": "sec",
            },
            origin=origin,
        )
    )
    _assert_parses(
        telegram.format_release_announcement_md(
            "v1.4.2", "Adds the pre-market report.", origin=origin
        )
    )


def test_a_price_always_renders_a_decimal_point_and_escapes_it() -> None:
    """Even a round number formats as 8.00, so the metrics run can never be
    left unescaped."""

    line = telegram._format_metrics_line({"price": 8, "change_pct": 10, "relative_volume": 4})
    assert "$8\\.00" in line and "\\+10\\.0%" in line and "4\\.0" in line
    _assert_parses(line)


def test_strip_markdown_v2_gives_the_reader_words_not_markup() -> None:
    message = telegram.format_board_segment_md(
        [{"ticker": "SOUN", "tag": "RUNNING", "price": 8.42, "score": 88}],
        origin="https://runners.rati.chat",
    )
    plain = telegram.strip_markdown_v2(message)
    assert "\\" not in plain and "*" not in plain
    assert "$8.42" in plain and "score 88" in plain
    assert "$SOUN https://runners.rati.chat/t/SOUN" in plain


def test_send_post_fallback_sends_plain_text_rather_than_the_markup_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[urllib.request.Request] = []

    def _open(request: urllib.request.Request, timeout: int = 0) -> _FakeResponse:
        opened.append(request)
        if len(opened) == 1:
            return _FakeResponse(
                400, body=b'{"ok":false,"description":"Bad Request: can\'t parse entities"}'
            )
        return _FakeResponse(200, body=b'{"ok":true}')

    import urllib.request as _real_urllib

    monkeypatch.setattr(_real_urllib, "urlopen", _open)
    telegram.send_post(_config(), "\U0001f406 *SOUN* up 12\\.5%")
    fallback = _url_payload(opened[1])
    assert "parse_mode" not in fallback
    assert fallback["text"] == "\U0001f406 SOUN up 12.5%"


def test_release_notes_are_trimmed_before_escaping() -> None:
    """Slicing escaped text can cut a backslash off its character; slicing the
    raw note cannot."""

    message = telegram.format_release_announcement_md(
        "1.0.0", "." * (telegram.RELEASE_NOTES_LIMIT + 50), origin="http://app.test"
    )
    assert len(message) <= telegram.MAX_MESSAGE_CHARS
    _assert_parses(message)


def test_a_long_batch_drops_whole_cards_instead_of_splitting_one() -> None:
    message = telegram.format_board_segment_md(
        [
            {"ticker": f"TICK{index}", "tag": "RUNNING", "price": 8.42, "score": 61}
            for index in range(400)
        ],
        origin="https://runners.rati.chat",
    )
    assert len(message) <= telegram.MAX_MESSAGE_CHARS
    _assert_parses(message)
