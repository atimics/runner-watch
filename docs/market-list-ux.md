# Tight list, tagged rows, and the universe behind a ticker

Design note, 13 September 2026. This records the list/top-bar redesign and the
removal of the market-wide Map tab. It revises the shared screen contract in
[market-screens/README.md](market-screens/README.md) and builds on the identity
work in [research/pseudonymous-identity-design.md](research/pseudonymous-identity-design.md).

A static preview lives at [market-list-preview.html](market-list-preview.html).

## The problem

The shared screen replaced a dense (overloaded) scanner card with a nearly empty
row, and kept a **Map** tab on the list screen that duplicates the per-ticker map
already shown on the ticker detail page. Two failures at once:

- **Too little signal in the list.** The current row in `market_screen.html`
  (`.ticker`) is a circle mark, ticker, company, price, change, arrow. The model
  already computes an action state, a stage, a rug level, relative volume and a
  15-minute move; none of it reaches the list, so the list cannot do its only job
  — help someone decide *what to open*.
- **A redundant navigation surface.** `market_screen.html` renders a List/Map
  toggle. The Map view (`map_connections`, `.map-subjects`) is a gallery of every
  ticker's map, while each ticker's real map (`_stock_map.html`, `ticker-map.js`)
  is already a section of the ticker detail page. The market-wide Map is an index
  of destinations, not a destination.

The old design did not have this problem; it had the opposite one. The deleted
`scanner.html` was a 14-column table (setup, rug, state, price, move, 52w fall,
15m, RVOL, $ volume, short float, borrow, filing, action). The legacy Pulse row
(`ticker-row.js`) carried five metrics (rank/Pulse/setup/RVOL/15m) plus a stage
badge, a trade-state badge, a thesis cue, a rug label and a catalyst sentence.
That is a feature vector with a hyperlink, not a row.

The redesign keeps what the old rows got right, deletes what they got wrong, and
gives the list one job.

## Principles

1. **One question per layer.** List answers "should I look?". Detail answers
   "should I act?". The universe answers "who is behind this?".
2. **Tags, not tables.** The model's decision becomes one colored tag. The
   feature vector never appears as a row of numbers.
3. **The top bar is status, navigation and filter.** Anything global lives there
   so the list body is only content.
4. **At most two supporting metrics.** RVOL and the 15-minute move are the two
   that precede a runner. Rank, score and setup stay in the detail page.
5. **A map is a place, not a tab.** The bubble universe is entered from a ticker.

## Information architecture

```
Top bar        brand · market switcher · session clock · breadth filters · search · account
   │
List           tight tagged rows                     "should I look?"
   │
Ticker detail  chart · your Call · key facts         "should I act?"
   │
Universe       orbiting actors, clusters, evidence   "who is behind it?"
```

Removed: the list-screen List/Map toggle, the `.map-subjects` gallery, and
`?view=map` as a top-level screen.

## The tag taxonomy

The backend has three signals and the list should express exactly one of them.

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
| **SETUP** | blue | `trade_state` ARMED, or `stage` in {EARLY, BUILDING} | Conditions building, not moving yet |
| **WATCH** | neutral | everything else | Noted, nothing decided |

Risk is a **secondary mark**, not a second badge: a small shield/dot after the
tag only when `rug_level` is GUARDED or worse. It never repeats the tag text.
The horizon (15m/60m) rides inside the tag only when a directional thesis exists.

## The list row

Desktop, one line (~64 px), four columns:

```
[ RUNNING ]  SOUN   SoundHound AI      $8.42  +18.3%   ╱╱╲╱   3.4×   15m +7.1%
             └ identity            ┘   └ price/change ┘  spark  RVOL    15m
```

Mobile, two lines (~72 px):

```
[ RUNNING ]  SOUN                $8.42  +18.3%
SoundHound AI · 3.4× · 15m +7.1%      ╱╱╲╱
```

Row contents, and nothing else:

1. **Action tag** — fixed width so rows align and scan vertically.
2. **Ticker + company** — company truncates before the ticker does.
3. **Price + change** — tabular, green/red.
4. **Sparkline** — shared percentage scale across the list (the legacy
   `ticker-row.js` mini-chart already does this correctly).
5. **RVOL and 15m** — small, labeled, hidden when unknown. These are the two
   predictive numbers; they are the only numbers besides price on the row.
6. **Relative age** — subtle, right edge. Freshness is a fact about the row.

Explicitly excluded from the list: rank, Pulse score, setup score, sector, short
interest, borrow, dollar volume, 52-week fall, the catalyst sentence and the
thesis description. All of these remain one tap away in detail.

## The top bar

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ RATi●   [ Stocks | Memecoins | Sports ]   ●REG 15:42 ET · 2m   ●3 running      │
│                                                ○9 setup  ○2 avoid   ⌕   ◉      │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Market switcher** stays a segmented control; it is the primary axis.
- **Session clock** (`_market_clock.html`, `/api/market-clock`) moves up from the
  page body. PRE/REG/AH/OVN plus the exchange time.
- **Freshness** ("Updated 2m ago") moves up with it.
- **Breadth filters** are the tag legend *and* the filter: `● 3 running`,
  `○ 9 setup`, `○ 2 avoid`. The chip colors match the row tags, clicking filters
  the list, and the counts make the market's mood readable without scrolling.
  This is the "move relevant info into the top bar" answer: the tag system's
  index is the top bar.
- **Search** moves into the bar (icon that expands on mobile). The list body no
  longer has a title, an eyebrow, a List/Map toggle or a search block.
- **Account** stays at the far right.

## The bubble universe

The per-ticker map becomes a first-class destination, entered from the ticker
detail page under the chart (a single **Explore the universe** band) rather than
from a market-wide tab. It reuses what already works:

- `_stock_map.html` + `ticker-map.js` for stocks (people, filings, activity).
- `_memecoin_replay.html` + `memecoin-replay.js` for coins (wallet clusters).
- `market-map.js` rendering: subjects at the centre, actors orbiting by weight,
  links colored by the latest reported action, keyboard-navigable bubbles.

Changes to make it a place rather than a section:

- Full-bleed width instead of the current 290 px side panel.
- Centre bubble = the ticker; orbit = actors; **cluster rings** group related
  actors so the "universe" reads at a glance.
- The existing filing-time slider stays and filters the whole universe.
- Selecting a bubble opens the existing evidence sheet.

## Progressive disclosure

| Layer | Question | Content | Budget |
| --- | --- | --- | --- |
| Top bar | What is the market doing? | session, freshness, breadth | 1 line |
| List | Should I look? | tag, identity, price/change, spark, RVOL, 15m | 1–2 lines |
| Detail | Should I act? | chart, Call terms, key facts, filings | one screen |
| Universe | Who is behind it? | actors, clusters, evidence, timeline | one screen |

## What we keep from each era

Keep from the old cards: the action badge, the mini sparkline with a shared
scale, RVOL and 15m, the relative age, and the rug signal as a compact mark.
Keep from the current screens: one shared template for all three markets, the
circle mark with tones, the calm dark palette, the accessible detail page, live
refresh, and the Call flow.

Discard: the 14-column scanner table, the repeated five-metric comparison strip,
the dual stage+trade-state badges, the list-screen Map gallery, and the
page-level title / View toggle / search block.

## Migration plan

1. `market_screens.py` — compute `tag`, `tag_tone` and `risk` in `row()` from
   `stage`, `trade_state` and `rug_level`. Stop calling `map_connections()` for
   the list view.
2. `market_screen.html` — delete `.views` and the body search/heading; move the
   session clock, freshness and search into a `_market_topbar.html` partial.
3. `listing()` / `_simple_board` — drop the `map` view. Redirect an incoming
   `?view=map` to the list (or to the ticker's universe when a subject is known)
   so saved links keep working.
4. `market-screen.css` — add tag, top-bar and filter-chip styles; port the badge
   palette already in `mobile.css` (`stage-early/building/running/extended`,
   `rug-*`) instead of inventing new colors.
5. List rendering — reuse the `ticker-row.js` sparkline, delete the
   `scoredComparison` strip from the list path (keep it in detail).
6. Detail — add the **Explore the universe** entry; keep `_stock_map.html`.
7. Tests — update `test_browser_market_screens.py`, `test_market_navigation.py`,
   `test_live_screens.py` and `docs/market-screens/README.md`.

## Open questions

- **Tag precedence** when `stage=RUNNING` but `trade_state=AVOID`: AVOID wins.
  Confirm that is the intended read.
- **Other markets.** Sports and memecoins need their own tag mapping (game state,
  coin risk) behind the same `{tag, tag_tone, risk}` interface. Define the shared
  minimum, then map per market.
- **Filter persistence.** The 60-second surface refresh replaces the list DOM;
  the active filter must survive it (the code already preserves search and focus).
- **Chip counts.** Should the top-bar counts be "in the saved board" or "in the
  current filter"? Proposed: always the whole board, so the filter is honest.
