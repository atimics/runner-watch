from runner_watch.universe import (
    _screen_entries,
    normalize_symbol,
    parse_custom_symbols,
    parse_nasdaq_listed,
    parse_other_listed,
)


def test_custom_symbols_are_clean_and_unique() -> None:
    assert parse_custom_symbols("aapl, BRK.B  aapl;tsla") == ["AAPL", "BRK-B", "TSLA"]
    assert normalize_symbol(" brk.b ") == "BRK-B"


def test_nasdaq_parser_removes_funds_tests_and_warrants() -> None:
    header = (
        "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
        "Round Lot Size|ETF|NextShares\n"
    )
    text = header + """GOOD|Good Company Common Stock|Q|N|N|100|N|N
FUND|Example ETF|G|N|N|100|Y|N
TEST|Test Company|S|Y|N|100|N|N
WARR|Company Warrants|S|N|N|100|N|N
File Creation Time: 0824202618:00|||||||
"""
    entries = parse_nasdaq_listed(text)
    assert [item.symbol for item in entries] == ["GOOD"]


def test_other_parser_uses_yahoo_symbol_format() -> None:
    header = (
        "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
        "Test Issue|NASDAQ Symbol\n"
    )
    text = header + """BRK.B|Berkshire Hathaway Common Stock|N|BRK.B|N|100|N|BRK.B
File Creation Time: 0824202618:00|||||||
"""
    entries = parse_other_listed(text)
    assert entries[0].symbol == "BRK-B"
    assert entries[0].exchange == "NYSE"


def test_screen_entries_keep_listed_stocks_and_drop_otc() -> None:
    quotes = [
        {"symbol": "PENNY", "shortName": "Penny Inc.", "exchange": "NCM"},
        {"symbol": "OTCF", "shortName": "OTC Inc.", "exchange": "PNK"},
        {"symbol": "BRK.B", "shortName": "Dots Inc.", "exchange": "NYQ"},
    ]
    entries = _screen_entries(quotes)
    assert [item.symbol for item in entries] == ["PENNY", "BRK-B"]
