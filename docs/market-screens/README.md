# Shared market screens

Stocks, Memecoins and Sports use one template, one stylesheet and one interaction
script. The market changes the content.

## Top bar

The bar carries everything global so the list body stays content-only: the RATi
brand, the market switcher, the session clock (PRE / REG / AH / OVN), data
freshness, the state filter chips, search, and the account link. The chips
(`1 running`, `1 setup`, `1 avoid`, …) are the tag legend and the filter at once;
clicking one hides every row whose tag differs.

## List

Each row is one line: the colored action tag, the ticker and company, price and
change, a shared-scale sparkline, and the score. The tag collapses `stage`,
`trade_state` and `rug_level` into one decision — `AVOID`, `EXTENDED`,
`RUNNING`, `SETUP` or `WATCH` — highest precedence first. A high-risk rug shows a
small mark on the tag, never a second badge.

Lists and live games refresh every minute. Refresh keeps search text, the active
filter and keyboard focus. The server selects public display fields before
rendering the screen or returning a quote or chart.

## Ticker page

One ordered story, four screens top to bottom:

1. **Chart** — price history, the movement period, the filing marker.
2. **Map** — the bubble universe: people, wallet clusters and filings around this
   ticker.
3. **Metrics = score** — the five drivers, the penalties and the formula behind
   the row's score.
4. **Comments** — avatar reactions and Flash reports.

The Call action sits after the metric panel.

## Removed

The market-wide **Map** tab is retired. A map belongs to one ticker, so it lives
inside the ticker page. An incoming `?view=map` renders the list instead; saved
links keep working.

Source receipts, provider names, model details, logs, credit counters and
collection diagnostics belong in internal tools. Keep these fields outside the
shared screen contract.

The screenshots in this directory were captured from local previews with sample
data on September 12, 2026, before the redesign. They are historical. A current
static preview of the list, tags and score lives at
[../market-list-preview.html](../market-list-preview.html).
