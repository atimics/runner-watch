import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from runner_web.telegram import AnimationDeliveryError, TelegramConfig, send_animation

CONFIG = TelegramConfig("fixture-token", "fixture-channel")


def test_animation_requires_its_own_enable_switch(monkeypatch):
    monkeypatch.delenv("TELEGRAM_MEMECOIN_ALERTS", raising=False)
    with pytest.raises(AnimationDeliveryError):
        send_animation(CONFIG, b"GIF89a", "caption", opener=lambda *_: pytest.fail("sent"))


def test_animation_upload_contains_gif_caption_and_configured_destination(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")

    def receiver(request, timeout):
        assert request.full_url.endswith("/sendAnimation")
        assert request.get_header("Content-type").startswith("multipart/form-data; boundary=")
        assert b'name="animation"; filename="token-replay.gif"' in request.data
        assert b"fixture-channel" in request.data
        assert b"GIF89atest" in request.data and b"New memecoin detected" in request.data
        assert timeout == 30
        return BytesIO(json.dumps({"ok": True, "result": {"message_id": 19}}).encode())

    assert send_animation(CONFIG, b"GIF89atest", "New memecoin detected", opener=receiver) == 19


@pytest.mark.parametrize("reply", [{"ok": False, "error_code": 403}, {}, {"ok": True}, []])
def test_api_errors_and_missing_acknowledgements_are_not_success(monkeypatch, reply):
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")
    with pytest.raises(AnimationDeliveryError) as error:
        send_animation(
            CONFIG,
            b"GIF89atest",
            "caption",
            opener=lambda *_args, **_kw: BytesIO(json.dumps(reply).encode()),
        )
    assert "fixture-token" not in str(error.value)


def test_rate_limit_carries_retry_time_and_hides_the_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")

    def receiver(request, **_):
        raise HTTPError(
            request.full_url,
            429,
            "limited",
            {},
            BytesIO(b'{"ok":false,"parameters":{"retry_after":123}}'),
        )

    with pytest.raises(AnimationDeliveryError) as error:
        send_animation(CONFIG, b"GIF89atest", "caption", opener=receiver)
    assert error.value.status == "retry" and error.value.retry_after == 123
    assert "fixture-token" not in str(error.value)


def test_transport_failure_stays_uncertain(monkeypatch):
    monkeypatch.setenv("TELEGRAM_MEMECOIN_ALERTS", "1")

    def receiver(*_, **__):
        raise TimeoutError("fixture-token")

    with pytest.raises(AnimationDeliveryError) as error:
        send_animation(CONFIG, b"GIF89atest", "caption", opener=receiver)
    assert error.value.status == "uncertain"
    assert "fixture-token" not in str(error.value)
