# Runner Watch

Runner Watch is a low-priced stock scanner for finding unusual price and volume movement early.
It uses free Yahoo Finance data through `yfinance`. It does not need a broker login or a paid API.

The public beta is at [stonks.rati.foundation](https://stonks.rati.foundation). Its mobile-first
app has three main views: **Pulse** for live penny-stock intelligence, a compact ticker page with
the chart and primary-source evidence, **Radar** for automatic activity-based alerts, and **Alpha**
for the community heart ranking and subscriber research reports. Pulse paginates as the user
scrolls. Radar works before login and merges into the passkey profile later.

The public scanner defaults to listed US penny stocks from $0.20 to $5 with market caps below
about $2B. It combines Yahoo's strongest movers, most active names, and largest losers before
checking daily liquidity and 5-minute bars. A second mode widens the price ceiling to $20. A third
mode only keeps stocks down at least 60% from a 90-day or 52-week high. OTC is excluded.

## Setup, rug risk, and trade state

Runner Watch no longer treats every signal as bullish. Every saved scan has three separate outputs:

- **Setup score:** could price move soon?
- **Rug risk:** could dilution, treasury, ownership, a halt, or bad liquidity trap the trade?
- **Trade state:** `WATCH`, `ARMED`, `TRIGGERED`, `MANAGE`, `AVOID`, or `EXIT`

A strong setup can still be `AVOID`. Risk filings subtract from Pulse instead of adding attention.
Critical rug risk and hard vetoes block the decision gate. A prior trigger becomes `EXIT` when price
structure breaks or new risk crosses the block level.

The ticker evidence gate counts independent evidence families instead of individual calculations.
Volume, momentum, VWAP, breakout, and the bar-derived pressure estimate are one **market** family;
SEC filings, news coverage, and public/community activity are separate families. Three families are
needed for confirmation, market evidence is always required, and risk vetoes still win. The full
receipt keeps both the family result and the supporting market checks.

The ticker detail view also receives an empirical base-rate receipt. After at least 20 prior
observations, the app compares the current value with one same-ticker, same-session, same-clock
observation per past trading session over a 120-day lookback. Until enough matched history exists,
it says that the baseline is still learning instead of showing a confident percentile.

Daily history covers one year. Drawdowns are measured over 20 days, 90 days, and 52 weeks. A 60%
drawdown only creates a crash candidate. It must reclaim VWAP with improving momentum before it can
become `TRIGGERED`.

The SEC Company Facts worker rotates through watched issuers. It stores point-in-time cash, operating
cash flow, current assets and liabilities, debt, public float, and shares outstanding. The risk engine
uses those facts for approximate cash runway, share growth, current ratio, and debt-to-cash. Missing
facts stay unknown and add an uncertainty warning; they never become a bullish assumption.

## EDGAR intelligence

The public **Pulse** is prepared by a background worker, so opening the page does not start a
market-wide scan. The worker:

- refreshes the SEC's official exchange-listed CIK-to-ticker map
- polls the SEC's current-filings Atom feed every 45 seconds
- deduplicates issuer and reporting-person entries by accession number
- parses structured Form 4 ownership XML, including post-trade holdings and footnote context
- treats transaction code `P` as a reported purchase that still needs stake and financing context
- keeps Schedule 13D and 13G neutral while recording structured ownership concentration when present
- flags offering, registration, late-report, Form 144, 8-K, 6-K, and ownership filings
- adds delayed Yahoo price, relative volume, and runner score as confirmation
- saves scored events for the compact penny-stock feed and ticker evidence timeline
- samples the observed price again after 1 hour, 1 day, and 5 days
- marks scanner runners that have no recent matching SEC filing

The SEC listener declares its user agent and stays below the SEC's published request-rate limit.
The delayed outcome labels preserve what was known at filing time, giving future ranking models
clean examples without rewriting history after a move is already known.

## Source ingestion

All collectors use one ingestion pipe. Every source fetch writes a durable run record with its
source, feed, status, timing, counts, content hash, and error. Raw responses can remain compressed
in `source_documents`. Normalized quotes, events, issuer facts, entity links, and macro observations
keep their source and collection times. A failed projection is rolled back while its failed run stays
visible for diagnosis.

The source registry records owner, terms, credentials, cadence, stale limits, storage, display, and
attribution rules. Use `/api/ingestion/status` to see healthy, stale, idle, pending, failed, and
disabled feeds. The tables do not replace the app's normal read tables; they provide one audit path
behind them.

Market providers now sit behind canonical typed contracts and an explicit provider registry. The
scanner still receives its normal data frames, but each routed result carries provider, as-of time,
collection time, warnings, quality notes, and fallback history. Partial responses are never silently
mixed across providers. Live chart reads share topic refreshes, keep a bounded last-known-good
value, and expose source and freshness metadata through the chart APIs. Database changes are
tracked with numbered forward migrations. See
[the provider and live-data runtime](docs/provider-runtime.md).

The Nasdaq Trader halt collector archives the RSS response and stores versioned halt state once a
minute during the extended US session. It is off by default while the feed terms are reviewed. Set
`NASDAQ_TRADE_HALTS_ENABLED=true` to opt in. An active halt is a hard risk veto.

The researched and prioritized plan for adding halts, quotes, fundamentals, news, biotech events,
short activity, and market context is in [the data source ingestion roadmap](docs/data-source-roadmap.md).
The end-to-end path from collectors to product evidence is in the
[data source flow map](docs/source-flow.md).

## Alpha reports

Each profile can heart a ticker once. Active unique hearts determine the Alpha order, and the wolf
marks the current leader. After the leader remains stable for three minutes, the report worker
builds a source-bound structured report for that ticker. Full reports are only rendered for users
whose `plan` is `subscriber`.

Bull and bear votes are separate from hearts and do not change the Alpha order. Signed-in users can
also leave short comments. Public comment names are stable adjective-animal aliases chosen from a
salted hash, while account names stay private. Set `COMMENT_PSEUDONYM_SALT` to a stable private value
in each deployed environment before comments are opened to users.

The report worker stays queued until `OPENAI_API_KEY` is configured. It uses the Responses API with
strict structured output, sends only stored market and filing evidence, and defaults to the model in
`AI_REPORT_MODEL` (`gpt-5.6`). A missing key never falls back to fake generated copy.

Commissioned reports are authored by **Flash ⚡**, Runner Watch's first AI KOL. Flash is a durable
public identity in position 1 of a planned four-position model ladder. Its current engine is
`z-ai/glm-5.3`; changing the engine later does not rename Flash or rewrite old reports. The other
ladder positions are not active yet. Set Flash's engine with `FLASH_MODEL` when a model is promoted.
The older `OPENROUTER_RESEARCH_MODEL` setting remains a compatibility fallback.

Flash uses one OpenRouter call. It does not run a web search or an agent loop. Before the call,
Runner Watch ranks and deduplicates its stored SEC filing
text, named reporting people, issuer facts, news, social reports, corporate events, prior risk
states, and market history. It fills up to 80% of the configured model context with relevant data,
without padding the prompt when less evidence exists, and reserves the rest for reasoning and the
report. The default GLM 5.3 context setting is 1,048,576 tokens. Override it with
`OPENROUTER_RESEARCH_CONTEXT_TOKENS`, change the fill ratio with
`OPENROUTER_RESEARCH_CONTEXT_FILL_RATIO`, or change the output cap with
`OPENROUTER_RESEARCH_OUTPUT_TOKENS`.

The report starts with an opinionated thesis, then explains who the company is, who the named people
or entities are, why each one appears in a filing, what the filings mean, what supports the thesis,
what could rug it, and what remains unknown. Source text is explicitly treated as evidence rather
than instructions.

The main screen ranks stocks with a **runner score**. The score looks for:

- volume that is unusual for the same time of day
- a fresh increase in the last 15 minutes
- positive 5-minute and 15-minute price movement
- momentum acceleration instead of momentum alone
- a move above the prior day's high
- price holding near the current session high and above intraday VWAP
- recent traded value, not only full-session dollar volume
- fresh data and a move that is not already too extended

It also cuts the score for falling short-term momentum, a pullback from the session high, weak
bar closes, and price below VWAP. The market score remains a readable screening rule. It is not
presented as a probability.

## Integer Rust ranker

Stonks keeps a complete, versioned training record for its learned ranker:

- each scan has one `scan_run` and saves every intraday candidate, not only the displayed top 40
- all feature inputs, missing values, baseline rank, catalyst context, and quote times are saved
- Yahoo daily and 5-minute bars fetched by the web app are deduplicated into `market_bars`
- every distinct SEC response body fetched by the listener is saved in `source_documents`
- each candidate is labeled by whether price touches +8% or −4% first in the next 60 minutes
- a row is called a timeout only when archived bars cover the full hour
- a bar touching both levels is conservatively labeled down and marked as ambiguous
- a three-way fixed-point logistic model predicts up, down, and timeout; the normal worker waits for
  160 complete groups and 5,000 labels; manual experiments must override both limits explicitly
- the oldest 80% of complete groups train the model, the next 10% calibrates its probabilities, and
  the newest 10% is an untouched test set
- learned probabilities and expected return are stored with the exact model ID
- the web worker collects a penny-stock scan every 30 minutes on weekdays from 4 a.m. to 8 p.m. ET

The model core is written in Rust and uses integers during normalization, training, softmax,
calibration, evaluation, and inference. Input features use thousandths, probabilities use parts per
million, and returns use basis points. This makes the same model artifact replay identically across
machines. The Python layer only prepares database rows, calls the Rust binary, and stores results.

The learned model remains in **shadow** promotion status. Its displayed chance means “estimated
chance of hitting +8% before −4% within 60 minutes.” Gross expected return is calculated from all
three outcomes. When a trained prediction exists, Pulse can use its score inside the composite
order; the hand-written score is the fallback, and deterministic risk vetoes still win.

Inspect or train it locally:

```bash
uv run stonks-ranker status
uv run stonks-ranker train --horizon 60m
```

Export the same complete candidate groups to the generic `crlplrimes` dataset contract:

```bash
uv run stonks-ranker export-crl data/stonks-crl-60m.csv --horizon 60m
```

The status API is available at `/api/ranker/status`. Raw source storage will grow over time, so a
long-running deployment should eventually move archived bars and documents to object storage.

`/api/capabilities` combines live source, worker, model, evidence-gate, and base-rate modes without
exposing credential names or values. Clients can use it instead of hardcoding which features are
available in a deployment.

## AI KOL calls

The calibrated ranker can now publish selective, permanent paper calls through **Flash ⚡**. Flash's
public identity and current inference model are separate from the internal ranker that supplies the
numeric signal. A call freezes its ticker, signal-model ID, confidence, expected return, contract,
time, and entry price. Repeated scans cannot reset the entry. Pulse shows active lightning tags with
net paper PnL, and ticker pages show the complete call receipt.

These existing paper calls benchmark Flash's fixed signal policy; they do not claim that GLM 5.3
personally authored the call. Flash's GLM 5.3 attribution applies to its commissioned research. A
future model battle can add model-authored calls without mixing them with this older signal track.

Flash uses the same `+8% before -4% within 60 minutes` contract as the shadow ranker. It can abandon
a stock only when a later frozen prediction crosses its fixed abandon rule. An abandoned call keeps
receiving the original 60-minute benchmark, so a model cannot hide a bad call by leaving early.
Calls use a fixed $1,000 paper amount and subtract a conservative 50 basis point round-trip cost.
They are research results, not trade recommendations.

Flash is the only seeded KOL slot today. Its records include ladder position, inference provider,
and inference model, and each commissioned report snapshots those fields. This keeps future model
promotions honest: an old GLM 5.3 report remains an old GLM 5.3 report. Human hearts remain separate
from AI reactions. Scorecards are available at `/api/kols`, and ticker call history is available at
`/api/t/{ticker}/kol-calls`.

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
- The public beta uses one PostgreSQL node with daily volume snapshots. It is not highly available
  yet; a managed or replicated PostgreSQL service is the next database reliability step.
- Subscriber entitlements are stored in the user record; billing automation is not connected yet.
- This is a research tool, not financial advice or an automatic buy signal.

Always confirm price, spread, volume, halt status, and news in a live broker before trading.

## Production layout and budget

Fly runs two stateless 512 MB web machines and one 1 GB background worker. PostgreSQL owns durable
application data. Redis shares the short-lived Pulse, Radar, and Alpha caches, applies rate limits
across both web machines, and carries encrypted research jobs between web and worker processes.
The old SQLite volume is kept only for rollback during the migration window.

The low-cost layout is about $19–$21 per month at light traffic: about $6.40 for PostgreSQL, $6.64
for both web machines, $5.92 for the worker, and usage-based Redis at $0.20 per 100,000 commands.
Network, snapshot, and Redis use can add a small amount. A Fly-managed PostgreSQL node would raise
the starting total to roughly $53–$58 per month.

SQLite remains supported for local development. To copy it to an empty PostgreSQL database:

```bash
uv run stonks-migrate-sqlite \
  --source data/runner-watch.db \
  --database-url "$DATABASE_URL"
```

## Checks

```bash
uv run pytest
uv run ruff check .
```
