from __future__ import annotations

import pytest

from runner_node.cloud_source import RatiCloudSource, RemoteScannerSource, normalize_scanner_url


def test_remote_scanner_urls_require_https_except_for_loopback() -> None:
    assert normalize_scanner_url("https://scanner.example.com/") == "https://scanner.example.com"
    assert normalize_scanner_url("http://127.0.0.1:9000") == "http://127.0.0.1:9000"
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_scanner_url("http://scanner.example.com")
    with pytest.raises(ValueError, match="without an API path"):
        normalize_scanner_url("https://scanner.example.com/api/v1/scans")


def test_remote_scanner_reads_bounded_receipts_with_its_token(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_get(url: str, token: str = "") -> dict[str, object]:
        captured.update(url=url, token=token)
        return {
            "receipts": [
                {"id": "one", "source": "live", "rows": []},
                {"id": "two", "source": "live", "rows": []},
            ]
        }

    monkeypatch.setattr("runner_node.cloud_source._get_json", fake_get)

    receipts = RemoteScannerSource().scans("https://scanner.example.com", "private-token")

    assert [receipt["id"] for receipt in receipts] == ["one", "two"]
    assert captured == {
        "url": "https://scanner.example.com/api/v1/scans",
        "token": "private-token",
    }


def test_rati_source_normalizes_free_candidates(monkeypatch) -> None:
    monkeypatch.setattr(
        "runner_node.cloud_source._get_json",
        lambda _url: {
            "updated_at": "2026-08-30T12:00:00+00:00",
            "rows": [
                {
                    "ticker": "rati",
                    "price": 1.25,
                    "change_pct": 8.5,
                    "pulse_label": "Moving",
                    "case_confidence": 0.72,
                    "case_thesis": "Source evidence",
                }
            ],
        },
    )

    receipt = RatiCloudSource().scans()[0]

    assert receipt["id"] == "rati-cloud-2026-08-30T12:00:00+00:00"
    assert receipt["rows"][0] == {  # type: ignore[index]
        "ticker": "RATI",
        "score": 72.0,
        "rug_score": 0.0,
        "rug_level": "UNKNOWN",
        "trade_state": "Moving",
        "price": 1.25,
        "change_pct": 8.5,
        "relative_volume": None,
        "state_reason": "Source evidence",
    }
