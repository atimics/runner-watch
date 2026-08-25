# Runner Watch

Runner Watch is a low-priced stock scanner for finding unusual price and volume movement early.
It uses free Yahoo Finance data through `yfinance`. It does not need a broker login or a paid API.

The public beta is at [stonks.rati.foundation](https://stonks.rati.foundation). Its mobile-first
app has three main views: **Pulse** for live penny-stock intelligence, a compact ticker page with
the chart and primary-source evidence, and **Radar** for a personal passkey-protected watchlist.
Public signal pages and social preview cards remain available from the profile menu.

The public scanner defaults to listed US penny stocks from $0.20 to $5 with market caps below
about $2B. It combines Yahoo's strongest movers and most active low-priced stocks before checking
daily liquidity and 5-minute bars. A second mode widens the price ceiling to $20. OTC is excluded.

## EDGAR intelligence

The public **Pulse** is prepared by a background worker, so opening the page does not start a
market-wide scan. The worker:

- refreshes the SEC's official exchange-listed CIK-to-ticker map
- polls the SEC's current-filings Atom feed every 45 seconds
- deduplicates issuer and reporting-person entries by accession number
- parses structured Form 4 ownership XML and reserves “insider buy” for transaction code `P`
- flags offering, registration, late-report, Form 144, 8-K, 6-K, and ownership filings
- adds delayed Yahoo price, relative volume, and runner score as confirmation
- saves scored events for the compact penny-stock feed and ticker evidence timeline
- samples the observed price again after 1 hour, 1 day, and 5 days
- marks scanner runners that have no recent matching SEC filing

The SEC listener declares its user agent and stays below the SEC's published request-rate limit.
The delayed outcome labels preserve what was known at filing time, giving future ranking models
clean examples without rewriting history after a move is already known.

The main screen ranks stocks with a **runner score**. The score looks for:

- volume that is unusual for the same time of day
- a fresh increase in the last 15 minutes
- positive 5-minute and 15-minute price movement
- a move above the prior day's high
- price holding near the current session high
- enough dollar volume to enter and exit more easily
- fresh data and a move that is not already too extended

## Shadow ranker

Stonks now keeps a complete, versioned training record without changing the public order:

- each scan has one `scan_run` and saves every intraday candidate, not only the displayed top 40
- all feature inputs, missing values, baseline rank, catalyst context, and quote times are saved
- Yahoo daily and 5-minute bars fetched by the web app are deduplicated into `market_bars`
- every distinct SEC response body fetched by the listener is saved in `source_documents`
- scan outcomes use archived bars after 1 hour, the next session, and the fifth later session
- a small listwise linear model trains only after at least 20 complete scan groups and 200 labels
- learned scores are stored in `ranker_predictions` with the exact model ID
- the web worker collects a penny-stock scan every 30 minutes on weekdays from 4 a.m. to 8 p.m. ET

The learned model remains in **shadow** status. The hand-written runner score continues to control
the public order and acts as the fallback when there is no trained model.

Inspect or train it locally:

```bash
uv run stonks-ranker status
uv run stonks-ranker train --horizon 1h
```

Export the same complete candidate groups to the generic `crlplrimes` dataset contract:

```bash
uv run stonks-ranker export-crl data/stonks-crl-1h.csv --horizon 1h
```

The status API is available at `/api/ranker/status`. Raw source storage will grow over time, so a
long-running deployment should eventually move archived bars and documents to object storage.

## Start the dashboard

This project uses Python 3.11–3.13. With [uv](https://docs.astral.sh/uv/) installed:

```bash
uv sync --extra dev --python 3.13 --no-editable
uv run streamlit run app.py
```

Open the local address shown in the terminal. Choose **Sample data** and run a scan first.
Sample mode is fake and lets you test the screen without internet access.

To run the public web app locally:

```bash
APP_ORIGIN=http://127.0.0.1:8080 RP_ID=127.0.0.1 COOKIE_SECURE=0 \
  uv run --no-sync uvicorn runner_web.main:app --host 127.0.0.1 --port 8080
```

For real data, choose **Live Yahoo data** and one of these ticker lists:

- **Quick starter list:** fast, but it cannot find a runner outside its saved list.
- **Full US market:** downloads the official Nasdaq Trader symbol directory, runs a daily
  liquidity filter, then checks the most active survivors with 5-minute bars.
- **My ticker list:** scans only the symbols you enter.

## Use the command line

Try the fake data:

```bash
uv run runner-watch --sample
```

Scan a custom watchlist:

```bash
uv run runner-watch --universe custom --symbols "ACHR ASTS IONQ RKLB SOUN"
```

Scan the broad market and save the top 50 rows:

```bash
uv run runner-watch --universe broad --scan-cap 1000 --top 50 --format csv --output runners.csv
```

Run `uv run runner-watch --help` for all filters.

## What “early” means here

An **EARLY** result has a strong score while still below an 8% move from the prior close.
**BUILDING** means the evidence is good but the move is farther along. **EXTENDED** means the
stock may already be crowded or parabolic. These labels are rules, not predictions.

The scanner uses 5-minute bars from the last five trading days. Current volume is compared
with the median volume reached by the same clock time on earlier days. This makes the number
more useful during pre-market than a simple comparison with a full day's average volume.

## Important limits

- Yahoo Finance is unofficial, can be delayed, and sometimes returns missing or wrong bars.
- A broad free scan can be slow or hit rate limits. Raise the scan cap in steps.
- The tool does not know the live bid/ask spread, current halt state, float, or all market news.
- The saved quick list will age. Use the full list or your own symbols for better coverage.
- The public beta uses one Fly volume. It is durable, but it is not yet a multi-region database.
- This first beta has no account recovery or backup-passkey screen. Losing the only passkey loses
  access to that account.
- This is a research tool, not financial advice or an automatic buy signal.

Always confirm price, spread, volume, halt status, and news in a live broker before trading.

## Checks

```bash
uv run pytest
uv run ruff check .
```
