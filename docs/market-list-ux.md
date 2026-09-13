# Tight list, one score, and the universe behind a ticker

Design note, 13 September 2026. This records the list/top-bar redesign, the
composite score, and the removal of the market-wide Map tab. It revises the
shared screen contract in [market-screens/README.md](market-screens/README.md)
and builds on the identity work in
[research/pseudonymous-identity-design.md](research/pseudonymous-identity-design.md).

A static preview lives at [market-list-preview.html](market-list-preview.html).

## The problem

The shared screen replaced a dense (overloaded) scanner card with a nearly empty
row, and kept a **Map** tab on the list screen that duplicates the per-ticker map
already shown on the ticker detail page. Two failures at once:

- **Too little signal in the list.** The current row in `market_screen.html`
  (`.ticker`) is a circle mark, ticker, company, price, change, arrow. The model
  already computes an action state, a stage, a rug level and nine score terms;
  none of it reaches the list, so the list cannot do its only job — help someone
  decide *what to open*.
- **A redundant navigation surface.** `market_screen.html` renders a List/Map
  toggle. The Map view (`map_connections`, `.map-subjects`) is a gallery of every
  ticker's map, while each ticker's real map (`_stock_map.html`, `ticker-map.js`)
  is already a section of the ticker detail page. The market-wide Map is an index
  of destinations, not a destination.

The old design had the opposite problem. The deleted `scanner.html` was a
14-column table. The legacy Pulse row (`ticker-row.js`) carried five metrics
(rank/Pulse/setup/RVOL/15m) plus a stage badge, a trade-state badge, a thesis cue,
a rug label and a catalyst sentence. That is a feature vector with a hyperlink,
not a row.

The redesign keeps what the old rows got right, deletes what they got wrong, and
gives the list one job. The five numbers collapse into **one score**, and the five
stay visible where there is room to explain them: the ticker page.

## Principles

1. **One question per layer.** List answers "should I look?". Ticker answers
   "should I act?". The universe, inside the ticker, answers "who is behind this?".
2. **Tags and a score, not tables.** The model's decision becomes one colored
   tag; its magnitude becomes one score. The feature vector never appears as a
   row of numbers in the list.
3. **The top bar is status, navigation and filter.** Anything global lives there
   so the list body is only content.
4. **One score from five metrics.** The list shows the score. The ticker page
   shows the five inputs and the formula behind it.
5. **A map is a place, not a tab.** The bubble universe is entered from a ticker.

## Information architecture

```
Top bar        brand · market switcher · session clock · breadth filters · search · account
   │
List           tight tagged rows + score            "should I look?"
   │
Ticker page    chart · map · metrics = score        "should I act?" then "who is behind it?"
```

Removed: the list-screen List/Map toggle, the `.map-subjects` gallery, and
`?view=map` as a top-level screen.

The ticker page is one ordered story, top to bottom:

1. **Chart** — price history, the filing marker, the movement period.
2. **Map** — the bubble universe for this ticker (`_stock_map.html` /
   `_memecoin_replay.html`, reusing `ticker-map.js` / `market-map.js`).
3. **Metrics = score** — the five inputs, their weights and contributions, and
   the resulting score, followed by the Call action and the company/community
   support sections.

This also fixes the current order, where the map block renders *above* the chart.

## The tag taxonomy

The backend has three signals and the row should express exactly one of them.

- `stage` (`scoring.py`): `EARLY`, `BUILDING`, `RUNNING`, `EXTENDED`, `WATCH`.
- `trade_state` (`risk.py`): `WATCH`, `ARMED`, `TRIGGERED`, `MANAGE`, `AVOID`, `EXIT`.
- `rug_level` (`risk.py`): `LOW`, `GUARDED`, `HIGH`, `CRITICAL`.

Showing `stage` **and** `trade_state` as two badges is what made the old rows
noisy and contradictory ("EARLY" next to "AVOID"). Collapse them into one action
tag, highest precedence first:

| Tag | Tone | Derived from | Means |
| --- | --- | --- | --- |
| **AVOID** | red | `trade_state` in {AVOID, EXIT}, or rug HIGH/CRITICAL | Risk wins, even if it can still pump |
| **EXTENDED** | orange | `stage` EXTENDED | Already ran; chase risk |
| **RUNNING** | green | `trade_state` in {TRIGGERED, MANAGE}, or `stage` RUNNING | In play now |
| **SETUP** | blue | `trade_state` ARMED, or `stage` in {EARLY, BUILDING} | Conditions building |
| **WATCH** | neutral | everything else | Noted, nothing decided |

**Precedence rule (confirmed):** AVOID always wins. A high score with
`trade_state=AVOID` shows **AVOID**, and the ticker page explains why in the
score breakdown and signals/risks.

Risk is a secondary mark, not a second badge: a small shield after the tag only
when `rug_level` is GUARDED or worse. It never repeats the tag text.

## The score

The list shows one number. The ticker page shows where it came from. The score is
a weighted composite of five normalized components, each 0–100:

| # | Component | Inputs | Weight |
| --- | --- | --- | --- |
| 1 | **Volume** | relative volume, recent relative volume | 25 |
| 2 | **Momentum** | 5m move, 15m move, acceleration | 25 |
| 3 | **Move quality** | change curve, breakout, range position, VWAP, close location | 20 |
| 4 | **Liquidity** | session and recent dollar volume | 15 |
| 5 | **Catalyst** | latest SEC filing / news event | 15 |

```
score = round( Σ (weight_i × component_i / 100) × freshness − penalties , 0 … 100 )
```

- `freshness` is the existing staleness multiplier from `scoring.py`.
- `penalties` are the existing extension/parabola/VWAP-break terms, shown
  separately so the arithmetic is honest.
- The five components are the same terms `score_runner` already uses, grouped and
  named so a person can read them. Component 5 (Catalyst) is the slot where a
  sixth source such as **LLM sentiment** later plugs in: add a component, add a
  weight, bump the score policy version. Nothing else changes.

The ticker page renders each component as a row: name, raw value, a 0–100 bar,
the weight, and its contribution, then the subtotal, penalties, freshness and
final score. This is the "how the score is calculated" panel.

**Versioning.** The weights and component list live in policy (alongside
`product_policy.py`) as `SCORE_POLICY_VERSION`, so a score can always be traced
to the formula that produced it. This matches how `FEATURE_SCHEMA_VERSION` works
for the ranker.

## The list row

Desktop, one line (~66 px):

```
[ RUNNING ]  SOUN   SoundHound AI     $8.42  +18.3%   ╱╱╲╱   84
             └ identity            ┘   └ price/change ┘ spark  score
```

Mobile, two lines (~74 px):

```
[ RUNNING ]  SOUN                 $8.42  +18.3%
SoundHound AI · score 84              ╱╱╲╱
```

Row contents, and nothing else:

1. **Action tag** — fixed width so rows align and scan vertically.
2. **Ticker + company** — company truncates before the ticker does.
3. **Price + change** — tabular, green/red.
4. **Sparkline** — shared percentage scale across the list.
5. **Score** — the single number, with a thin meter. Colour is a quiet magnitude
   band (strong / building / watch), never competing with the action tag.
6. **Relative age** — subtle, right edge.

Explicitly excluded from the list: rank, Pulse score, setup score, sector, short
interest, borrow, dollar volume, 52-week fall, the catalyst sentence and the
thesis description. All of these remain one tap away in the ticker page.

## The top bar

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ RATi●   [ Stocks | Memecoins | Sports ]   ●REG 15:42 ET · 2m   ●3 running      │
│                                                ○9 setup  ○2 avoid   ⌕   ◉      │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Market switcher** stays a segmented control; it is the primary axis.
- **Session clock** (`_market_clock.html`, `/api/market-clock`) moves up from the
  page body: PRE/REG/AH/OVN plus the exchange time.
- **Freshness** ("Updated 2m ago") moves up with it.
- **Breadth filters** are the tag legend *and* the filter: `● 3 running`,
  `○ 9 setup`, `○ 2 avoid`. Chip colors match the row tags, clicking filters the
  list, and the counts make the market's mood readable without scrolling. The tag
  system's index is the top bar.
- **Search** moves into the bar (icon that expands on mobile).
- **Account** stays at the far right.

The list body then carries no title, eyebrow, View toggle or search block.

## The bubble universe

The per-ticker map becomes the middle of the ticker page, not a market-wide tab.
It reuses what already works:

- `_stock_map.html` + `ticker-map.js` for stocks (people, filings, activity).
- `_memecoin_replay.html` + `memecoin-replay.js` for coins (wallet clusters).
- `market-map.js` rendering: subjects at the centre, actors orbiting by weight,
  links colored by the latest reported action, keyboard-navigable bubbles.

Changes to make it a place rather than a section:

- Full-bleed width instead of the current 290 px side panel.
- Centre bubble = the ticker; orbit = actors; **cluster rings** group related
  actors so the universe reads at a glance.
- The existing filing-time slider stays and filters the whole universe.
- Selecting a bubble opens the existing evidence sheet.
- A single **Explore the universe** affordance under the chart anchors the entry.

## Progressive disclosure

| Layer | Question | Content | Budget |
| --- | --- | --- | --- |
| Top bar | What is the market doing? | session, freshness, breadth | 1 line |
| List | Should I look? | tag, identity, price/change, spark, score | 1–2 lines |
| Ticker · chart | What happened? | price history, movement period, filing marker | one chart |
| Ticker · map | Who is behind it? | actors, clusters, evidence, timeline | one screen |
| Ticker · metrics | Why this score? | 5 components, weights, formula, signals/risks | one panel |
| Ticker · Call | Should I act? | Call terms, entry, settlement, reward | one action |

## What we keep from each era

Keep from the old cards: the action badge, the mini sparkline with a shared
scale, the score, the relative age, and the rug signal as a compact mark. Keep
from the current screens: one shared template for all three markets, the circle
mark with tones, the calm dark palette, the accessible detail page, live refresh,
and the Call flow.

Discard: the 14-column scanner table, the repeated five-metric comparison strip,
the dual stage+trade-state badges, the list-screen Map gallery, and the
page-level title / View toggle / search block.

## Migration plan

1. `market_screens.py` — compute `tag`, `tag_tone`, `risk` and `score` in `row()`
   from `stage`, `trade_state`, `rug_level` and the score model. Stop calling
   `map_connections()` for the list view.
2. `market_screen.html` — delete `.views` and the body search/heading; move the
   session clock, freshness and search into a `_market_topbar.html` partial; move
   the chart block above the map block so the order is chart → map → metrics.
3. Score panel — add a `score_breakdown` view model (five components, their raw
   values, weights, contributions, penalties, freshness) and render it on the
   ticker page. Put the weights and component list in policy as
   `SCORE_POLICY_VERSION`.
4. `listing()` / `_simple_board` — drop the `map` view. Redirect an incoming
   `?view=map` to the list (or to the ticker's universe when a subject is known)
   so saved links keep working.
5. `market-screen.css` — add tag, score-meter, top-bar and filter-chip styles;
   port the badge palette already in `mobile.css` (`stage-early/building/running/
   extended`, `rug-*`) instead of inventing new colors.
6. List rendering — reuse the `ticker-row.js` sparkline; delete the
   `scoredComparison` strip from the list path (it becomes the detail score panel).
7. Tests — update `test_browser_market_screens.py`, `test_market_navigation.py`,
   `test_live_screens.py` and `docs/market-screens/README.md`.

## Open questions

- **Score derivation.** Two options: (a) expose the *existing* `score_runner`
  result grouped into five named contributions — no ranking change; or (b) make
  the five normalized components the score itself with explicit weights — a
  ranking change. Recommendation: ship **(a)** now so nothing re-ranks, then move
  to **(b)** when the sixth component (LLM sentiment) lands and the versioned
  policy exists.
- **Other markets.** Sports and memecoins need their own tag mapping and, for
  coins, their own score components (rug/liquidity/holders). Define the shared
  `{tag, tag_tone, risk, score}` interface, then map per market.
- **Filter persistence.** The 60-second surface refresh replaces the list DOM;
  the active filter must survive it (the code already preserves search and focus).
- **Chip counts.** Should the top-bar counts be "in the saved board" or "in the
  current filter"? Proposed: always the whole board, so the filter is honest.
