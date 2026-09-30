# Stock attention glyph

Presentation-only change for the stock list and detail screen. Scoring, model
probabilities, ordering, policy, and the existing `state_tag()` derivation are
unchanged. WATCH, SETUP, RUNNING, EXTENDED, AVOID and PAUSED retain their chips,
colors, risk marks, filters and precedence. Sports, wallet nodes and run-state derivation keep their existing presentation.
The ticker map now uses the shared glyph contract for its central stock node.

## Visual contract

- Under 40 attention points: small segmented ring.
- 40 to below 70: large segmented ring.
- 70 and above: large solid pie, the same diameter as the large ring.
- Blue = market; purple = SEC filings plus news; cyan = external social.
  Slice angles use positive post-freshness contributions divided by their sum,
  **before** the final attention score cap. Zero/missing shares never become
  arbitrary equal thirds. Neither internal comments nor risk deductions appear
  as slices. The original fine-grained score breakdown remains available.
- Full perimeter: green positive, red negative, gray neutral/mixed, dashed gray
  unknown. This is the existing **filing sentiment**, not current price direction,
  a crowd aggregate, probability, or newly trained sentiment model.
- Center: no marker for assessed low risk; orange for guarded/medium (25–<50);
  red for high/critical (50+); `?` for unavailable/invalid risk. A hard veto or
  halt urgency is red. Conflicting recognized risk readings take the higher band.
- Known zero contributions get a quiet empty outline; missing breakdowns get a
  dashed empty outline. Unknown data is not an attractive-looking low score.

## What the check verifies

There is no existing per-stock human approval record in this path. The check is
explicitly **Verified evidence — automated**: the existing evidence gate must
have `state=ready`, no blockers, and the current eligibility must be `eligible`,
with no hard veto. Running status, price movement, numerical attention and an
untrusted raw `verified` flag cannot award it. This presents the existing gate;
it does not add a new approval policy or claim manual review, identity
verification, regulatory approval, safety, or expected returns. The check has
its own name slot; it never replaces the run-state chip.

The detail view reads its authoritative top-level evidence gate. List refreshes
replace server-rendered marks; detail refreshes use the existing accepted-response
event to update the map glyph and separate header check. A missing updated indicator
clears verification rather than preserving stale clearance. There is no extra
poller or database query.

## Accessibility and verification

The glyph and approval mark have accessible names. The list has a keyboard/tap
accessible indicator key. On the ticker page, the map itself provides the visible
text breakdown and key; there is no separate attention card above the price chart. A tooltip is supplementary, never the sole explanation. With JavaScript disabled, a server-rendered description and filing links remain
available inside the map's noscript fallback. Client refresh uses DOM APIs,
fixed color keys and textContent, not untrusted HTML. Desktop and mobile list
layout, risk and sentiment independence, true solid fill, check revocation and
unchanged status/filter behavior have regression coverage.

No schema migration, dependency, scoring-threshold or production setting changes.

## Ticker-map integration

The map's central node has the same small-ring / large-ring / solid silhouette,
three contribution groups, full filing-sentiment border and center risk marker.
The stock symbol and attention points sit below the face, leaving a solid pie
filled through the center except for its small optional risk badge.

Selecting a slice opens its fine-grained trace in the existing map selection
area (filings + news includes both traces). The perimeter and risk marker can
be selected independently; neither becomes an attention slice or deduction.
Keyboard arrows, Enter/Space, Escape and touch selection remain supported.
Wallet navigation, filing pagination, filing scrubbing, chart markers and orbit
behavior remain on their existing paths. Indicator-only updates preserve wallet
DOM nodes and selected filings, and refreshes include sentiment/risk changes even
when attention is unchanged. The map can render attention before filings load,
and missing/failed filing responses do not remove the glyph.

## Memecoin glyph

The token list and token wallet map share the stock glyph's CSS, SVG geometry,
attention bands, color keys and risk marker. Blue is market, purple is chain
evidence and cyan is external social. The label and score sit below the ring.
The key explains the token meaning, and each map part opens its text reading.
Arrow keys move between parts; Enter/Space select; Escape returns to the overview.

The token adapter uses saved `attention_score` or `score`, with positive
`market`, `chain_event` and `social_search` contributions from `score_components`
or `score_detail.drivers`. Its perimeter reads explicit `chain_sentiment`.
Its center uses saved risk fields through the shared risk rules above.
Stock verification remains scoped to the stock evidence gate.

The current quote and receipt pipeline supplies no numeric attention assessment.
These tokens therefore have a dashed empty ring, an unavailable attention label,
an unknown evidence tone and a question mark for unknown risk. Receipt counts,
price changes, penalties and forecast probabilities never become ring slices.
A saved assessment can populate the same glyph later. This change is presentation
only; scoring and collection continue on their existing paths.

Accepted detail refreshes update the glyph and keep a selected part when it is
still available. Selected receipt links remain usable during a refresh. The
server also supplies the glyph description for the JavaScript-free fallback.

## Stocks on entity pages

Entity stock nodes use `stock_indicator()` and the shared SVG face renderer.
Their area follows the reported holding value, with a minimum readable size.
The scale uses all loaded stocks. Their labels show the stock symbol and
reported holding value or event count below the face. The ring uses the same
market / filings + news / external social groups, evidence-tone border and risk
marker as the stock row beneath it. Solid fill marks 70+ stock attention points.
The entity map key explains this holding-based size. Stocks awaiting a holding
value use the minimum size and show their event count. Missing breakdowns retain
the dashed state.

Each node is one keyboard-accessible link to the stock detail page, where the
ring parts have individual controls. The entity remains at the center, and the
map retains its filing links and connections to other wallets.

Phone and desktop maps show all loaded stocks, ordered by holding value.
Up to eight stocks share one orbit. Larger sets spread into an outward spiral,
with the largest holdings nearest the center. Each node retains its distance as
it orbits. Larger portfolios open around the largest holdings at a readable
size. Reduced-motion preferences keep the layout still.

The map supports drag, pinch, wheel and button zoom. Fit map restores the full
view. With the map focused, +/− zoom, arrows move and Home restores the view.
Dragging pauses the orbit while the pointer is down and preserves stock links
for ordinary taps. The holding size and stock glyph meaning stay steady at
every zoom level.

## Ratified

A stock is Ratified when it meets all five of RATi's basic standards
(`src/runner_web/stock_ratify.py`), from data already collected, at no extra
cost. It is not an endorsement, a guarantee or advice. The scanner's
"Verified evidence" gate is not one of them: it asks whether today's momentum
setup is backed up (volume, news, a current quote), so it fails every stock
while markets are closed.

1. Listed on NASDAQ, NYSE or NYSE American (not OTC), from the SEC listing map.
2. Up to date with SEC filings: a 10-Q or 10-K (or amendment) filed in the last
   135 days. A foreign private issuer (files 20-F, 40-F or 6-K and never 10-Q or
   10-K) is held to its own schedule instead: a 20-F or 40-F annual report filed
   in the last 490 days (twelve months between fiscal years plus the four-month
   filing deadline). Interim 6-K reports are not told apart from other 6-Ks, so
   only the annual report is read. The latest report is taken from the financial
   facts or the filing index, whichever is later, because IFRS filers may not
   use the XBRL tags the facts are read from.
3. At least 12 months of cash at the current operating burn, or not burning
   cash (operating cash flow zero or positive). Not applied to banks, lenders,
   insurers and real estate (SIC 6000-6799), whose lending runs through
   operating cash flow. This is decided once, in the issuer facts
   (`issuer_risk.py`: `runway_applies`), so the scanner's risk check,
   eligibility, Dash and ratification all read the same answer. A stock readers see without a SIC code is looked up
   first by the sector backfill, five a pass, and a missing code is retried
   after six hours instead of waiting out the 90-day refresh.
4. Share count up 25% or less over roughly a year.
5. No trading halt in the last 30 days and no delisting notice in the last 90.
   A domestic issuer's notice is 8-K item 3.01; item numbers are read from the
   EDGAR feed summary as filings arrive (migration 93), so notices filed before
   that are not seen. A foreign issuer reports its notice on 6-K, which has no
   item numbers: the 6-K's primary document is read as it arrives, and one that
   reports a deficiency, delisting determination or suspension is marked
   `listing-notice` (`edgar.is_listing_notice`). "Regained compliance" does not
   match. 6-Ks filed before this shipped are not seen, so a foreign issuer's
   trading standard reads "not checked yet" until the 6-K reading has covered
   90 days (`sec_delistings.foreign_notices_read`). Limit up-limit down and
   market-wide circuit breaker pauses (reason codes LUDP, LUDS, M, MWC*) stop
   trading because the price moved, not because of the company, and do not
   count as halts here.

### Which issuers each standard reads

| Standard | US domestic (10-Q/10-K/8-K) | Foreign private issuer (20-F/40-F/6-K) |
| --- | --- | --- |
| Listing | NASDAQ, NYSE, NYSE American | Same: this is a US-listing standard |
| Filings | 10-Q or 10-K within 135 days | 20-F or 40-F within 490 days; interim 6-Ks not read. With no 20-F or 40-F held, not checked yet (never "not met") |
| Cash | us-gaap facts; not applied to SIC 6000-6799 | ifrs-full facts (or us-gaap), in the reporting currency. SIC 6000-6799 not applied. With no SIC code read yet, not checked yet |
| Dilution | Shares outstanding, year on year | Same, usually from annual 20-F figures; ordinary shares, not ADSs |
| Trading | Nasdaq halts; 8-K item 3.01 | Nasdaq halts; 6-K listing notices read from text. Not checked yet until 6-K texts have been read for the whole 90 days |

Why a foreign issuer can read "not checked yet" where a domestic one reads a
result (`stock_ratify._foreign_inputs`, foreign issuers only):

- Interim results are furnished on 6-K, which cannot be told from other 6-Ks
  by form type, so a 6-K is never evidence of an up-to-date report. With no
  annual report held, the only evidence would be an interim 6-K.
- EDGAR gives foreign banks and insurers a SIC code like any filer, so the
  6000-6799 carve-out applies to them once the code is read. Until it is read,
  a foreign bank cannot be told from an operating company and its operating
  cash flow is not a burn, so cash is not judged. Whether EDGAR's codes are
  right for every foreign bank has not been checked against the live data.

The board shows a Ratified mark; the stock page lists every standard as met,
not met, not checked yet, or not applied. "Not applied" (a dash, with the
reason) is a standard that does not fit the company, such as cash runway for a
financial company: it is left out of the count ("4 of 4 met · 1 not applied")
and does not block. "Not checked yet" means missing data, and does block. Missing financials leave a standard unknown, so the
stock is not ratified.

## Flash forecast settlement and clocks

A Flash forecast is scored on the US regular session close (16:00
America/New_York), for every ticker. This is a rule of the Call, not of the
company, and it is unchanged. A foreign issuer (files 20-F, 40-F or 6-K and
never 10-Q or 10-K) can trade in its home market at other hours, so news can
move its shares before the US open. Its forecast card therefore adds
"Scored on the US session only..." (`market_forecasts.FOREIGN_SETTLEMENT_NOTE`).
Every forecast row carries `settlement_basis`. The card does not say whether a
gap happened: RATi holds no home-market prices. Forecasts already settled are
not touched.

Time zones. Market rules use America/New_York on purpose: the session clock
(`market_clock`), the halt feed (`nasdaq_halts`), the forecast close and the
"your day" boundary for Calls. Times shown to readers in those places carry an
explicit zone label ("ET", "EDT", "EST"), so a time is never shown as if it were
local. Not done: showing times in the reader's own zone. That needs the reader's
zone from the browser and would change the byte-for-byte output for US readers,
so it is left open.

