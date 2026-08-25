from __future__ import annotations

import argparse
import csv
import io
import json
from pathlib import Path

from runner_watch.market_data import YahooMarketData
from runner_watch.models import ScanResult, ScanSettings
from runner_watch.sample_data import SAMPLE_SYMBOLS, SampleMarketData
from runner_watch.scanner import RunnerScanner
from runner_watch.universe import broad_us_universe, parse_custom_symbols, starter_universe


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runner-watch", description="Scan stocks for unusual volume and momentum."
    )
    parser.add_argument("--sample", action="store_true", help="Use fake demo data.")
    parser.add_argument(
        "--universe", choices=["starter", "broad", "custom"], default="starter"
    )
    parser.add_argument("--symbols", default="", help="Comma- or space-separated custom symbols.")
    parser.add_argument("--min-price", type=float, default=0.50)
    parser.add_argument("--max-price", type=float, default=50.0)
    parser.add_argument("--min-volume", type=int, default=100_000)
    parser.add_argument("--min-dollar-volume", type=float, default=500_000)
    parser.add_argument("--scan-cap", type=int, default=300)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--format", choices=["table", "csv", "json"], default="table")
    parser.add_argument("--output", type=Path, help="Save output instead of printing it.")
    return parser


def _progress(done: int, total: int, label: str) -> None:
    if done == 0 or done == total:
        print(f"[{done}/{total}] {label}", flush=True)


def _plain_rows(result: ScanResult) -> list[dict[str, object]]:
    return [item.to_dict() for item in result.rows]


def _render_table(result: ScanResult) -> str:
    headings = ["TICKER", "SCORE", "STAGE", "PRICE", "CHANGE", "5M", "15M", "RVOL", "$ VOL"]
    rows = []
    for item in result.rows:
        rows.append(
            [
                item.ticker,
                f"{item.score:.1f}",
                item.stage,
                f"${item.price:.2f}",
                f"{item.change_pct:+.1f}%",
                f"{item.momentum_5m_pct:+.1f}%",
                f"{item.momentum_15m_pct:+.1f}%",
                f"{item.relative_volume:.1f}x" if item.relative_volume is not None else "—",
                f"${item.dollar_volume / 1_000_000:.1f}M",
            ]
        )
    widths = [
        max(len(str(value)) for value in [heading] + [row[index] for row in rows])
        for index, heading in enumerate(headings)
    ]
    lines = ["  ".join(heading.ljust(widths[index]) for index, heading in enumerate(headings))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(str(value).ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )
    return "\n".join(lines)


def _render(result: ScanResult, output_format: str) -> str:
    rows = _plain_rows(result)
    if output_format == "json":
        return json.dumps(rows, indent=2)
    if output_format == "csv":
        if not rows:
            return ""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()
    return _render_table(result)


def main() -> int:
    args = _parser().parse_args()
    warnings: list[str] = []
    if args.sample:
        provider = SampleMarketData()
        symbols = SAMPLE_SYMBOLS
    else:
        provider = YahooMarketData()
        if args.universe == "starter":
            symbols = [item.symbol for item in starter_universe()]
        elif args.universe == "broad":
            entries, warnings = broad_us_universe()
            symbols = [item.symbol for item in entries]
        else:
            symbols = parse_custom_symbols(args.symbols)

    settings = ScanSettings(
        min_price=args.min_price,
        max_price=args.max_price,
        min_avg_volume=args.min_volume,
        min_avg_dollar_volume=args.min_dollar_volume,
        max_symbols=args.scan_cap,
        top_n=args.top,
    )
    result = RunnerScanner(provider).scan(symbols, settings, progress=_progress)
    result.warnings[:0] = warnings
    text = _render(result, args.format)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"Saved {len(result.rows)} row(s) to {args.output}")
    else:
        print(text)
    for warning in dict.fromkeys(result.warnings):
        print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
