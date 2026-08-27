# RATi Runners and RATi Sports

RATi is an evidence-first prediction platform. RATi Runners scans low-priced stocks for unusual movement. RATi Sports compares a transparent team-form baseline with timestamped no-vig moneyline odds and keeps public paper-pick receipts.
It uses free Yahoo Finance data through `yfinance`. It does not need a broker login or a paid API.
Optional Fintel short and borrow data needs a Fintel API key and the matching account access.

RATi Runners is at [runners.rati.chat](https://runners.rati.chat), RATi Sports is at [sports.rati.chat](https://sports.rati.chat), and the old stock address remains available during migration. The mobile-first Runners
app has three main views: **Pulse** for live penny-stock intelligence, a compact ticker page with
the chart and primary-source evidence, **Radar** for fresh market and evidence changes, and **Alpha**
for public Calls. Signed-in users can spend Flash credits on private research and AI-written ticker
comments. Pulse paginates as the user scrolls. Radar is shared and does not build a reading profile.

RATi Sports currently covers MLB, NFL, NBA, and NHL schedules. A background worker stores event
state, season records, moneyline snapshots, source times, and a versioned baseline prediction. The
model compares team form plus a small home advantage with the market price after removing the
bookmaker margin. It does not promote preseason or exhibition games. Signed-in users can publish
paper picks with frozen odds; completed games settle them as wins, losses, or pushes. The app does
not place bets or connect to sportsbooks. ESPN remains a preview source for schedules, records, and
results. When the `ODDS_API_KEY` Fly secret is present, production requests Bovada, DraftKings,
FanDuel, BetMGM, and BetOnline moneylines through The Odds API instead of using the preview odds.
The worker requests the `h2h` market only, removes each book's margin, and uses the median of at
least three fresh two-sided lines as the model's market benchmark. Bovada remains a separate quote:
the game receipt shows its difference from the other-book consensus and freezes its price for paper
picks. Stale and incomplete lines are excluded. The worker takes at most three
snapshots per league slate: opening, pregame, and close. The 500-credit plan has a 450-credit working
limit and a protected 50-credit reserve. Quota headers are checked before paid calls, and cached odds
are served between snapshots. The key stays on the server and is never sent to the browser or logs.

Sports uses the same three public views as Runners. **Pulse** ranks current Lean and Watch
model-versus-market gaps. **Radar** shows material odds or model changes only for games that entered
Pulse. **Alpha** is the public Call ledger: each winner is treated like a ticker, its no-vig market
probability is treated like a price, and every paper Call keeps its entry odds, current mark, and
settled PnL. The old Receipts URLs redirect to Alpha. Sealed pregame model and odds records remain on
each game page as supporting evidence.

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

Daily bars can come from Massive (https://massive.com) instead of Yahoo. Set `MASSIVE_API_KEY` to
enable it. Massive's grouped daily endpoint returns one trading session for every listed US stock
in a single request, so the adapter caches whole sessions in a local SQLite store and the scan-time
budget stays small. Intraday 5-minute bars keep using Yahoo because the Massive entry plan is
end-of-day only. If the cache is cold or a fetch would exceed the scan budget, the registry falls
back to Yahoo for that request and records the attempt in provenance. Warm the cache with
`stonks-massive-backfill` (one run covers a year of sessions at the plan's 5 calls per minute, so
schedule it nightly). The worker runs its own hourly warm-up pass bounded by
`MASSIVE_BACKFILL_CALLS`, so the cache self-heals after every deploy. `MASSIVE_ENABLED`,
`MASSIVE_CALLS_PER_MINUTE`, `MASSIVE_MAX_SCAN_CALLS`, `MASSIVE_CACHE_PATH`, and
`MASSIVE_TIMEOUT_SECONDS` tune the collector. Call timing is coordinated machine-wide through
the shared cache, so the worker daemon and a manual backfill cannot exceed the plan's rate limit
together, and HTTP 429 responses are retried with backoff.

The Nasdaq Trader halt collector archives the RSS response and stores versioned halt state once a
minute during the extended US session. It is off by default while the feed terms are reviewed. Set
`NASDAQ_TRADE_HALTS_ENABLED=true` to opt in. An active halt is a hard risk veto. The production
configuration currently opts in, so `/api/capabilities` reports a blocking policy warning until the
catalog review state is explicitly approved.

The scanner can also show exchange-reported short interest, short float, days to cover, borrow fee,
and shares available. Set `FINTEL_API_KEY` to enable Fintel's documented API. The app refreshes only
the displayed scan rows, caches normalized results for 15 minutes, links each value to its source,
and keeps the settlement or observation time. It does not scrape Fintel or IBorrowDesk pages. A
missing key, missing account entitlement, or source error leaves the values unknown without
breaking the price scan. Use `FINTEL_SHORT_DATA_TTL_SECONDS`, `FINTEL_SHORT_DATA_MAX_SYMBOLS`,
`FINTEL_SHORT_DATA_TIMEOUT_SECONDS`, and `FINTEL_SHORT_DATA_WORKERS` to tune the collector, or set
`FINTEL_SHORT_DATA_ENABLED=false` to turn it off without removing the secret.

Short interest is an open-position count reported twice monthly. Daily short-sale volume is a
different measure and is not used as a substitute. Borrow fee and availability are intraday
securities-lending readings from one feed, not a market-wide count of all shares that can be
borrowed.

The researched and prioritized plan for adding halts, quotes, fundamentals, news, biotech events,
short activity, and market context is in [the data source ingestion roadmap](docs/data-source-roadmap.md).
The end-to-end path from collectors to product evidence is in the
[data source flow map](docs/source-flow.md).

## Alpha reports

Alpha is a public Call ledger. A signed-in user can make one open Call per ticker. Runner Watch
freezes the current server quote and time; the browser cannot submit or edit either value. Closing a
Call also uses the current server quote and time. Calls show percentage PnL only, with no implied
position size. Each account gets one automatic random animal name for Calls; there is no identity
picker or paid identity flow, and account names stay private. Comments use a random emoji identity
that is stable inside that ticker thread.

Bull, bear, heart, private position, and private case controls are not part of the product flow.
Signed-in users can ask Flash to draft a short public ticker comment from their perspective. There
is no free-text comment input. Each ticker thread gives the account a stable random two-emoji alias.
Account names stay private, and the emoji cannot be changed from the thread.

Reports are authored by **Flash ⚡**. Flash's current engine is `z-ai/glm-5.3`, routed through
OpenRouter. The model label is shown beside Flash in the interface. Changing the engine later does
not rename Flash or rewrite old report snapshots.

Flash makes one source-bound OpenRouter request. The stored evidence covers catalysts, financing,
market and liquidity, filings, people, news, and social context. Unapproved source links are removed,
and the deterministic rug engine keeps veto power outside the model.

Before those stages, Runner Watch ranks and deduplicates its stored SEC filing text, named reporting
people, issuer facts, news, social reports, corporate events, prior risk states, and market history.
It fills up to 80% of the configured model context with relevant data without padding it when less
evidence exists. Override the context with `RESEARCH_CONTEXT_TOKENS`, change the fill ratio with
`RESEARCH_CONTEXT_FILL_RATIO`, or change the output reserve with
`RESEARCH_OUTPUT_RESERVE_TOKENS`. The older `OPENROUTER_RESEARCH_*` context names remain accepted so
existing deployments can migrate without losing their settings.

The first daily Flash report for a ticker costs 100 Flash and gives its generator a one-hour private
alpha window. Other users cannot generate a duplicate during that lock. The owner can publish the
report early to make it public immediately and earn 50 Flash. Otherwise, the completed report opens
automatically when the hour ends. Failed reports refund the 100 Flash charge and release the daily
lock. Each user can explicitly claim 100 Flash once per UTC day; missed claims do not accrue. The
server uses its own provider key. The worker queue contains only a report ID, and the user does not
provide or store a model-provider key.

The report starts with an opinionated thesis, then explains who the company is, who the named people
or entities are, why each one appears in a filing, what the filings mean, what supports the thesis,
what could rug it, and what remains unknown. Source text is explicitly treated as evidence rather
than instructions. Model-selected citations are checked against the supplied source URLs. A hard
deterministic `AVOID` or `EXIT` state overrides the generated wording.

## Flash wallet

`/billing` is now the Flash wallet. It has no subscription or Pro gate. A user starts at zero and
must press the daily claim button to receive 100 Flash. The claim is available once per UTC day and
missed days are not backfilled. A daily Flash report costs 100, an AI-written ticker comment costs
10, publishing during the private alpha hour earns 50 once, and a winning sports Call earns 25 once
its final score settles. A profitable Runners Call earns 10 Flash, rising to 20 at a 20% return and
30 at a 30% or higher return. Ledger references make claims, charges, refunds, and rewards safe to
retry without paying twice.

Buying Flash credit packs is intentionally paused. Historical subscription columns remain only so
existing databases migrate safely. The public product plan, including features that are explicitly
not being built, lives at `/roadmap` and `/api/roadmap`.

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

Stonks keeps a compact, versioned training record for its learned ranker:

- each scan has one `scan_run` and records every intraday candidate, not only the displayed top 40
- each candidate is quantized once into `ranker_training_examples`; training does not reload full
  snapshot rows
- full snapshots remain available for 150 days for receipts, signals, and 120-day base rates
- Yahoo daily and 5-minute bars fetched by the web app are deduplicated into `market_bars`
- every distinct SEC response body fetched by the listener is saved in `source_documents`
- each candidate is labeled by whether price touches +8% or −4% first in the next 60 minutes
- a row is called a timeout only when archived bars cover the full hour
- a bar touching both levels is conservatively labeled down and marked as ambiguous
- a three-way fixed-point logistic model predicts up, down, and timeout; the trainer waits for 160
  complete groups and 5,000 labels, then uses at most the latest 320 complete groups
- the trainer is a separate process, runs at most every six hours, and waits for 16 new groups before
  rebuilding an existing model
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

The status API is available at `/api/ranker/status`. Bars are retained for 60 days, full scan
snapshots for 150 days, compact training examples for one year, and raw source documents for one
year. Long-term raw archives should live in object storage.

`/api/capabilities` combines live source, worker, model, evidence-gate, base-rate, training, and
promotion policy without exposing credential names or values. It also reports enabled sources that
have not passed review. Clients can use it instead of hardcoding deployment behavior. `/live`
reports only that the web process is running. `/health` and its `/ready` alias check the database
and minimum schema without depending on the worker. `/health/details` also requires a recent
healthy worker heartbeat. Fly routes traffic using the backward-compatible `/health` endpoint,
while the detailed endpoint lets external monitoring detect a dead or partly failed worker without
taking healthy web machines offline.

## Legacy model evaluations

The calibrated ranker can now publish selective, permanent paper calls through **Flash ⚡**. Flash's
public identity and current inference model are separate from the internal ranker that supplies the
numeric signal. A call freezes its ticker, signal-model ID, confidence, expected return, contract,
time, and entry price. Repeated scans cannot reset the entry. Pulse shows active lightning tags with
net paper PnL, and ticker pages show the complete call receipt.

These existing paper calls benchmark Flash's fixed signal policy; they do not claim that Flash's
current model personally authored the call. Model attribution applies to commissioned research. A
future model battle can add model-authored calls without mixing them with this older signal track.

Flash uses the same `+8% before -4% within 60 minutes` contract as the shadow ranker. It can abandon
a stock only when a later frozen prediction crosses its fixed abandon rule. An abandoned call keeps
receiving the original 60-minute benchmark, so a model cannot hide a bad call by leaving early.
Calls use a fixed $1,000 paper amount and subtract a conservative 50 basis point round-trip cost.
They are research results, not trade recommendations.

Flash is the only seeded KOL slot today. Its records include ladder position, inference provider,
and inference model, and each commissioned report snapshots those fields. This keeps future model
promotions honest: an old report keeps its original model snapshot. Human comments remain separate
from AI calls. Scorecards are available at `/api/kols`, and ticker call history is available at
`/api/t/{ticker}/kol-calls`.

These model evaluations are separate from user-created public Calls. User Calls live in
`community_calls`; private Flash research lives in `research_commissions`.

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
- Fintel short and borrow data needs an API subscription and may require separate feed access.
- A broad free scan can be slow or hit rate limits. Raise the scan cap in steps.
- The tool does not know the live bid/ask spread or all market news. Halt evidence is available only
  while the Nasdaq feed is enabled and fresh; its public-use terms still need approval.
- The saved quick list will age. Use the full list or your own symbols for better coverage.
- The public beta uses one PostgreSQL node with daily volume snapshots. It is not highly available
  yet; a managed or replicated PostgreSQL service is the next database reliability step.
- Flash credit packs cannot be purchased yet; only daily claims and earned rewards are live.
- This is a research tool, not financial advice or an automatic buy signal.

Always confirm price, spread, volume, halt status, and news in a live broker before trading.

## Production layout and budget

Fly runs two stateless 512 MB web machines, one 1 GB collection worker, and one 1 GB bounded ranker
trainer. PostgreSQL owns durable application data. Redis shares the short-lived Pulse, Radar,
Alpha, and chart caches, applies rate limits across both web machines, and carries encrypted
research jobs between web and worker processes.

`cloudflare-router/` is the small edge router for `runners.rati.chat` and
`sports.rati.chat`. Both public products share the same Fly deployment. The
router passes the original public host through so branding, passkeys, and
origin checks stay correct.

List charts send only time and price, use response compression, and cache their complete response
for one minute.

Set one shared `RATE_LIMIT_HASH_KEY` so rate-limit keys contain neither raw IP addresses nor account
IDs. Production sets `REQUIRE_RATE_LIMIT_HASH_KEY=1`, so a missing shared key stops startup instead
of silently weakening limits across machines. The old SQLite volume must be retired after the
migration check; it is not a standing backup.

The low-cost layout is about $25–$27 per month at light traffic: about $6.40 for PostgreSQL, $6.64
for both web machines, $11.84 for the worker and trainer, and usage-based Redis at $0.20 per 100,000
commands. Network, snapshot, and Redis use can add a small amount. A Fly-managed PostgreSQL node
would raise the starting total further.

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

The production browser sweep checks all 17 Runners, Sports, and account screen routes. It uses
current public ticker, caller, signal, research, and game records, fails screens slower than one
second, and saves a screenshot for each failure. A dynamic screen with no public record is called
out; the legacy Signal screen is skipped until production has a real public Signal to render.

```bash
scripts/test-live-screens
```

Run bounded staging bursts at 20, 40, and 80 concurrent requests:

```bash
uv run python scripts/probe_scalability.py https://staging.example.com
```

The probe refuses a host that does not look like staging or localhost unless
`--allow-production` is supplied explicitly.
