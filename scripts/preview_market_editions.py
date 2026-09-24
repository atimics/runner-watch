"""Render both editions with clearly labelled sample data, using production templates.

Run: uv run python scripts/preview_market_editions.py /tmp/report-editions
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

from jinja2 import ChainableUndefined, Environment, FileSystemLoader

from runner_web.market_reports import _metric_cards, _record_cards, _share
from runner_web.report_spotlight import decorate_edition

ROOT = Path(__file__).resolve().parents[1]


def sample_report(post: bool) -> dict:
    leaders = []
    for index, (ticker, score, move, volume, price) in enumerate(
        [
            ("DEMO", 64, 8.4, 4.2, 4.25),
            ("EXMP", 48, 5.8, 2.9, 8.6),
            ("SAMPLE", 31, -3.2, 1.8, 2.15),
        ]
    ):
        close = round(price * (1.06 if index < 2 else 0.988), 4)
        leaders.append(
            {
                "ticker": ticker,
                "rank": index + 1,
                "score": score,
                "change_pct": move,
                "relative_volume": volume,
                "price": price,
                "trade_state": "WATCH",
                "signals": ["Volume acceleration", "Near session high"],
                "risks": ["Thin liquidity"],
                "rug_score": 30,
                "board_status": "held" if post else None,
                "close_rank": index + 2,
                "close_price": close if post else None,
                "session_return_pct": (6 if index < 2 else -1.2) if post else None,
                "eod_forecast": {
                    "reference_price": price,
                    "target_price": round(price * 1.04, 2),
                    "direction": "up",
                    "close_price": close if post else None,
                    "status": ("hit" if index < 2 else "miss") if post else "pending",
                    "close_source": "Sample data" if post else None,
                    "reason": "Watch whether the opening volume supports the move.",
                    "review_reason": None,
                },
            }
        )
    metrics = {
        "candidates": 128,
        "green": 76,
        "red": 42,
        "median_relative_volume": 2.4,
        "high_risk": 18,
        "winners": 2,
        "losers": 1,
        "average_session_return_pct": 3.6,
    }
    feature = None
    if post:
        metrics["closing_breadth"] = {"candidates": 164, "green": 92, "red": 59}
        snapshot = copy.deepcopy(leaders[0])
        snapshot.update(
            price=4.505,
            change_pct=18.6,
            relative_volume=5.8,
            signals=["Volume acceleration", "Break above prior high", "Near session high"],
            risks=["Thin liquidity", "Cash use deserves a closer look"],
        )
        feature = {
            "ticker": "DEMO",
            "snapshot": snapshot,
            "universe": 164,
            "reason": (
                "+18.6% at the saved checkpoint · 5.8× relative volume · "
                "3 saved signals · 2 risk flags"
            ),
            "selection_parts": [
                {"label": "Price move", "points": 29.76, "max": 40},
                {"label": "Volume", "points": 24, "max": 25},
                {"label": "Signals", "points": 15, "max": 20},
                {"label": "Risk context", "points": 10, "max": 15},
            ],
            "company": {
                "name": "Demo Energy Systems",
                "industry": "Industrial energy storage",
                "exchange": "NASDAQ",
                "source_url": None,
            },
            "facts": [
                {
                    "label": label,
                    "value": value,
                    "unit": unit,
                    "period_start": start,
                    "period_end": "2026-06-30",
                    "filed_at": "2026-08-12",
                    "source_url": "#sample-data",
                }
                for label, value, unit, start in [
                    ("Cash", 84200000, "USD", None),
                    ("Total debt", 31600000, "USD", None),
                    ("Shares", 148000000, "shares", None),
                    ("Operating cash flow", -12600000, "USD", "2026-01-01"),
                ]
            ],
            "filings": [
                {
                    "form": "8-K",
                    "title": "Sample manufacturing update",
                    "filed_at": "2026-09-23",
                    "filing_url": None,
                },
                {
                    "form": "10-Q",
                    "title": "Sample quarterly results and cash position",
                    "filed_at": "2026-08-12",
                    "filing_url": None,
                },
            ],
        }
    report = {
        "id": "post" if post else "pre",
        "report_type": "post_market" if post else "pre_market",
        "label": "Post-market recap" if post else "Pre-market briefing",
        "report_day": "2026-09-23",
        "as_of": "2026-09-23T20:10:00+00:00" if post else "2026-09-23T08:16:00+00:00",
        "as_of_label": "4:10 PM ET" if post else "4:16 AM ET",
        "headline": "An active close, with two watch names ahead"
        if post
        else "DEMO leads the pre-market board",
        "summary": (
            "The watch board carried three names into the session. "
            "Two finished above the watch price."
        )
        if post
        else (
            "128 names in the saved scan. 76 were up, "
            "with volume concentrated in the opening leaders."
        ),
        "metrics": metrics,
        "leaders": leaders,
        "turns": [],
        "spotlight": feature,
        "forecast_record": {"label": "2–1", "decided": 3, "win_rate": 66.7, "pending": 0},
        "forecast_model": {"model_label": "Sample model"},
        "forecast_state": "complete",
        "desk_comments": [],
        "commentary_state": "complete",
        "analysis": {
            "headline": "Volume gave the move its shape" if post else "Watch the opening volume",
            "narrative": (
                "The saved checkpoints show where attention gathered. "
                "Compare the targets with the results, "
                "then follow the evidence into the company profile."
            )
            if post
            else (
                "The early board points to a small group of active names. "
                "Opening volume and the first pullback are the next useful checks."
            ),
            "points": [
                "Read price strength alongside the saved liquidity checks.",
                "Follow the source dates when comparing company facts.",
            ],
        },
    }
    report["metric_cards"] = _metric_cards(report)
    report["record_cards"] = _record_cards(report) if post else []
    report["share"] = _share(report)
    report["permalink"] = report["share"]["path"]
    decorate_edition(report)
    return report


def main() -> None:
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "output/report-editions")
    destination.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(ROOT / "web/templates"),
        autoescape=True,
        undefined=ChainableUndefined,
    )
    reports = [sample_report(True), sample_report(False)]
    for report in reports:
        markup = env.get_template("market_report_detail.html").render(
            report=report,
            app_origin="",
            runners_origin="",
            sports_origin="",
            static_version="preview",
            user=None,
            request={"state": {"csp_nonce": "preview"}, "url": {"path": report["permalink"]}},
        )
        markup = re.sub(
            r'<link rel="stylesheet" href="/static/([^?]+)\?[^\"]*">',
            lambda match: "<style>" + (ROOT / "web/static" / match[1]).read_text() + "</style>",
            markup,
        )
        markup = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", markup)
        preview = (
            '<aside id="sample-data" style="padding:10px 20px;text-align:center;'
            'font-size:12px;background:#292318;color:#e5ba7b">'
            "Design preview · Illustrative company and market data · "
            '<a href="pre.html">Pre-market</a> / <a href="post.html">Post-market</a></aside>'
        )
        markup = markup.replace("<body>", "<body>" + preview)
        (destination / f"{report['id']}.html").write_text(markup)
    print(destination.resolve())


if __name__ == "__main__":
    main()
