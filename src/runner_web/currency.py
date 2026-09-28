"""Amounts in the currency they were reported in.

Issuer facts keep their ISO 4217 unit; a EUR balance must never read as
dollars. Only unambiguous symbols are used, and other currencies show their
code ("CAD 1.2M"), so CAD, AUD and HKD are not mistaken for USD.
"""

from __future__ import annotations

import math
from typing import Any

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def amount(value: Any, currency: str | None = "USD", *, compact: bool = False) -> str:
    """ "$1,234.00", "€1.2M" or "CAD 1.2M"; an unknown currency is shown as USD."""

    number = _number(value)
    if number is None:
        return "Awaiting data"
    code = (currency or "USD").upper()
    sign = "-" if number < 0 else ""
    number = abs(number)
    text = f"{number:,.2f}"
    if compact:
        text = f"{number:,.0f}"
        for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
            if number >= divisor:
                text = f"{number / divisor:,.1f}{suffix}"
                break
    symbol = SYMBOLS.get(code)
    return f"{sign}{symbol}{text}" if symbol else f"{sign}{code} {text}"
