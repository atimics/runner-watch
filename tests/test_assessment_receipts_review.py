"""Chain patterns keep their complete proof through the public assessment."""

from bs4 import BeautifulSoup

from runner_web.market_assessments import assessment
from runner_web.market_screens import detail
from runner_web.memecoin_forensics import analyze_events
from tests.test_market_screens import render, sample
from tests.test_memecoin_forensics import swap


def chain_pattern():
    token = "A" * 44
    events = [
        {
            **swap(str(i + 2) * 88, "wallet", "buy" if i % 2 == 0 else "sell", second=i * 20),
            "token_address": token,
        }
        for i in range(4)
    ]
    finding = analyze_events(events)["findings"][0]
    coin = {**sample("memecoins"), "token_address": token, "findings": [finding]}
    return detail("memecoins", {"coin": coin}), finding


def test_round_trip_pattern_keeps_all_receipts_and_context_in_rendered_html():
    screen, finding = chain_pattern()
    driver = screen["item"]["assessment"]["drivers"][0]
    assert driver["id"] == finding["id"]
    assert driver["basis"] == "pattern"
    assert len(driver["evidence"]) == 4
    expected = [receipt["receipt_url"] for receipt in finding["evidence"]]
    assert [receipt["receipt_url"] for receipt in driver["evidence"]] == expected
    panel = BeautifulSoup(render(screen), "html.parser").select_one(".assessment-finding")
    assert panel.select_one("summary").get_text(strip=True) == "Pattern · 4 receipts"
    assert panel.select_one("p").get_text() == finding["explanation"]
    assert [item.select_one("a")["href"] for item in panel.select("li")] == expected
    assert [item.select("a")[1]["href"] for item in panel.select("li")] == [
        receipt["source_url"] for receipt in finding["evidence"]
    ]


def test_receipts_validate_each_url_and_publish_only_display_fields():
    result = assessment(
        "memecoins",
        {
            "token_address": "mint",
            "findings": [
                {
                    "token_address": "mint",
                    "evidence": [
                        {
                            "event_id": "saved",
                            "kind": "token_launch",
                            "receipt_url": "/api/memecoins/evidence/saved",
                            "source_url": "javascript:alert(1)",
                            "operator_secret": "private",
                        },
                        {
                            "event_id": "source",
                            "source_url": "https://solscan.io/tx/source",
                            "receipt_url": "//example.test/path",
                        },
                        {"source_url": "https://example.test/\npath", "receipt_url": "/account"},
                        "malformed receipt",
                    ],
                }
            ],
        },
    )
    receipts = result["drivers"][0]["evidence"]
    assert receipts == [
        {
            "event_id": "saved",
            "kind": "Token launch",
            "receipt_url": "/api/memecoins/evidence/saved",
            "source_url": None,
        },
        {
            "event_id": "source",
            "kind": "Event",
            "receipt_url": None,
            "source_url": "https://solscan.io/tx/source",
        },
    ]


def test_pattern_explanation_is_escaped_in_server_render():
    screen, _ = chain_pattern()
    screen["item"]["assessment"]["drivers"][0]["explanation"] = "<script>alert(1)</script>"
    panel = BeautifulSoup(render(screen), "html.parser").select_one(".assessment-finding")
    assert panel.select("script") == []
    assert panel.select_one("p").get_text() == "<script>alert(1)</script>"
