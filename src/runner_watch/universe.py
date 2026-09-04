from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from runner_watch.ingestion import SourceFetch, SourceFetchRecorder

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"


STARTER_SYMBOLS = [
    "AAL",
    "AAPL",
    "ACHR",
    "AFRM",
    "AI",
    "ALAB",
    "AMD",
    "AMZN",
    "ASTS",
    "AVGO",
    "BBAI",
    "BILI",
    "BITF",
    "BROS",
    "BTBT",
    "BYND",
    "CAVA",
    "CCL",
    "CELH",
    "CHPT",
    "CLSK",
    "COIN",
    "CORZ",
    "CRDO",
    "CRSP",
    "CVNA",
    "DJT",
    "DKNG",
    "DNA",
    "DUOL",
    "ENVX",
    "ETHA",
    "F",
    "FIG",
    "FUBO",
    "GME",
    "GOEV",
    "GOOGL",
    "GRAB",
    "HOOD",
    "HUT",
    "IBIT",
    "IONQ",
    "IREN",
    "JOBY",
    "LCID",
    "LUNR",
    "MARA",
    "META",
    "MSTR",
    "MU",
    "NBIS",
    "NIO",
    "NKLA",
    "NVDA",
    "OKLO",
    "OPEN",
    "ONDS",
    "ORCL",
    "OSCR",
    "PATH",
    "PLTR",
    "QBTS",
    "QUBT",
    "RGTI",
    "RIVN",
    "RKLB",
    "ROKU",
    "RXRX",
    "SAVA",
    "SBUX",
    "SERV",
    "SHOP",
    "SLV",
    "SMCI",
    "SNAP",
    "SOFI",
    "SOUN",
    "SPCE",
    "TIGR",
    "TMC",
    "TSLA",
    "TSM",
    "TSSI",
    "U",
    "UBER",
    "UPST",
    "VFS",
    "VKTX",
    "WBD",
    "WOLF",
    "XPEV",
    "ZETA",
    "ZM",
]


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    symbol: str
    name: str = ""
    exchange: str = ""
    is_fund: bool = False


def normalize_symbol(value: str) -> str:

    return value.strip().upper().replace(".", "-")


def parse_custom_symbols(value: str) -> list[str]:
    parts = re.split(r"[\s,;]+", value)
    return list(dict.fromkeys(normalize_symbol(part) for part in parts if part.strip()))


def _looks_like_common_stock(name: str) -> bool:
    blocked = (
        " warrant",
        " warrants",
        " unit",
        " units",
        " right",
        " rights",
        " preferred",
        " notes due",
        " bond",
        " debenture",
    )
    lowered = f" {name.lower()}"
    return not any(token in lowered for token in blocked)


def _read_pipe_rows(text: str) -> list[dict[str, str]]:
    rows = list(csv.DictReader(io.StringIO(text), delimiter="|"))
    return [
        row
        for row in rows
        if row and not any("File Creation Time" in str(value) for value in row.values())
    ]


def parse_nasdaq_listed(text: str, include_funds: bool = False) -> list[UniverseEntry]:
    entries: list[UniverseEntry] = []
    for row in _read_pipe_rows(text):
        symbol = normalize_symbol(row.get("Symbol", ""))
        name = row.get("Security Name", "").strip()
        is_fund = row.get("ETF", "N") == "Y"
        if not symbol or row.get("Test Issue") == "Y":
            continue
        if not include_funds and is_fund:
            continue
        if not _looks_like_common_stock(name):
            continue
        entries.append(UniverseEntry(symbol, name, "NASDAQ", is_fund))
    return entries


def parse_other_listed(text: str, include_funds: bool = False) -> list[UniverseEntry]:
    exchange_names = {"A": "NYSE American", "N": "NYSE", "P": "NYSE Arca", "Z": "Cboe"}
    entries: list[UniverseEntry] = []
    for row in _read_pipe_rows(text):
        raw_symbol = row.get("NASDAQ Symbol") or row.get("ACT Symbol", "")
        symbol = normalize_symbol(raw_symbol)
        name = row.get("Security Name", "").strip()
        is_fund = row.get("ETF", "N") == "Y"
        if not symbol or row.get("Test Issue") == "Y":
            continue
        if not include_funds and is_fund:
            continue
        if not _looks_like_common_stock(name):
            continue
        entries.append(
            UniverseEntry(symbol, name, exchange_names.get(row.get("Exchange", ""), "US"), is_fund)
        )
    return entries


def starter_universe() -> list[UniverseEntry]:
    return [UniverseEntry(symbol=symbol, name="Starter list") for symbol in STARTER_SYMBOLS]


def _screen_entries(quotes: list[dict[str, Any]]) -> list[UniverseEntry]:

    allowed_exchanges = {"ASE", "BTS", "NAE", "NCM", "NGM", "NMS", "NYQ", "PCX"}
    entries: list[UniverseEntry] = []
    for quote in quotes:
        symbol = normalize_symbol(str(quote.get("symbol", "")))
        exchange = str(quote.get("exchange", ""))
        if not symbol or exchange not in allowed_exchanges:
            continue
        entries.append(
            UniverseEntry(
                symbol=symbol,
                name=str(quote.get("shortName") or quote.get("longName") or ""),
                exchange=exchange,
            )
        )
    return entries


def penny_runner_universe(
    *,
    min_price: float = 0.20,
    max_price: float = 5.00,
    max_market_cap: float = 2_000_000_000,
    per_screen: int = 175,
    fetch_recorder: SourceFetchRecorder | None = None,
) -> tuple[list[UniverseEntry], list[str]]:

    warnings: list[str] = []
    overall_started_at = datetime.now(UTC)

    def record(fetch: SourceFetch) -> None:
        if fetch_recorder is None:
            return
        try:
            fetch_recorder(fetch)
        except Exception as exc:
            warnings.append(f"Could not record Yahoo universe fetch: {exc}")

    try:
        import yfinance as yf

        listed_exchanges = ["NCM", "NMS", "NGM", "NYQ", "ASE", "NAE", "PCX", "BTS"]
        query = yf.EquityQuery(
            "and",
            [
                yf.EquityQuery("eq", ["region", "us"]),
                yf.EquityQuery("is-in", ["exchange", *listed_exchanges]),
                yf.EquityQuery("gt", ["intradayprice", min_price]),
                yf.EquityQuery("lt", ["intradayprice", max_price]),
                yf.EquityQuery("gt", ["dayvolume", 100_000]),
                yf.EquityQuery("gt", ["avgdailyvol3m", 100_000]),
                yf.EquityQuery("gt", ["intradaymarketcap", 2_000_000]),
                yf.EquityQuery("lt", ["intradaymarketcap", max_market_cap]),
            ],
        )
        entries: dict[str, UniverseEntry] = {}
        size = max(25, min(per_screen, 250))
        for sort_field, sort_ascending in (
            ("percentchange", False),
            ("dayvolume", False),
            ("percentchange", True),
        ):
            started_at = datetime.now(UTC)
            try:
                response = yf.screen(
                    query,
                    size=size,
                    sortField=sort_field,
                    sortAsc=sort_ascending,
                )
                quotes = response.get("quotes", [])
                record(
                    SourceFetch.success(
                        source="yahoo",
                        feed="universe",
                        locator=(
                            f"yfinance://screen/{sort_field}/{'asc' if sort_ascending else 'desc'}"
                        ),
                        started_at=started_at,
                        payload=quotes,
                        content_type="application/json",
                        metadata={
                            "sort_field": sort_field,
                            "sort_ascending": sort_ascending,
                            "size": size,
                            "min_price": min_price,
                            "max_price": max_price,
                            "max_market_cap": max_market_cap,
                        },
                    )
                )
                for entry in _screen_entries(quotes):
                    entries.setdefault(entry.symbol, entry)
            except Exception as exc:
                direction = "ascending" if sort_ascending else "descending"
                warnings.append(f"Yahoo {sort_field} {direction} candidate screen failed: {exc}")
                record(
                    SourceFetch.failure(
                        source="yahoo",
                        feed="universe",
                        locator=(
                            f"yfinance://screen/{sort_field}/{'asc' if sort_ascending else 'desc'}"
                        ),
                        started_at=started_at,
                        error=exc,
                        metadata={
                            "sort_field": sort_field,
                            "sort_ascending": sort_ascending,
                            "size": size,
                        },
                    )
                )
        if entries:
            return list(entries.values()), warnings
        warnings.append("Yahoo returned no low-priced candidates.")
    except Exception as exc:
        warnings.append(f"Could not build the low-priced candidate list: {exc}")
        record(
            SourceFetch.failure(
                source="yahoo",
                feed="universe",
                locator="yfinance://screen",
                started_at=overall_started_at,
                error=exc,
                metadata={
                    "min_price": min_price,
                    "max_price": max_price,
                    "max_market_cap": max_market_cap,
                },
            )
        )

    warnings.append("Using the saved momentum list as a fallback.")
    return starter_universe(), warnings


def _download_text(url: str, timeout: int = 15) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "RunnerWatch/0.1"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def broad_us_universe(
    include_funds: bool = False,
    cache_dir: Path | None = None,
    cache_hours: int = 18,
) -> tuple[list[UniverseEntry], list[str]]:

    warnings: list[str] = []
    cache_dir = cache_dir or Path.cwd() / ".runner_watch_cache"
    cache_path = cache_dir / f"universe-funds-{int(include_funds)}.json"
    if cache_path.exists() and time.time() - cache_path.stat().st_mtime < cache_hours * 3600:
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            return [UniverseEntry(**item) for item in data], warnings
        except (OSError, ValueError, TypeError):
            warnings.append("The saved symbol list was damaged, so it will be downloaded again.")

    try:
        nasdaq = parse_nasdaq_listed(_download_text(NASDAQ_LISTED_URL), include_funds)
        other = parse_other_listed(_download_text(OTHER_LISTED_URL), include_funds)
        entries = list({item.symbol: item for item in nasdaq + other}.values())
        entries.sort(key=lambda item: item.symbol)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps([asdict(item) for item in entries], indent=2), encoding="utf-8"
        )
        return entries, warnings
    except (OSError, TimeoutError, ValueError) as exc:
        warnings.append(f"Could not refresh the Nasdaq symbol list: {exc}")

    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            warnings.append("Using an older saved symbol list.")
            return [UniverseEntry(**item) for item in data], warnings
        except (OSError, ValueError, TypeError):
            pass

    warnings.append("Using the starter list because the full symbol list is unavailable.")
    return starter_universe(), warnings
