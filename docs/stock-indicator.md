# Stock attention glyph

Presentation-only change for the stock list and detail screen. Scoring, model
probabilities, ordering, policy, and the existing `state_tag()` derivation are
unchanged. WATCH, SETUP, RUNNING, EXTENDED, AVOID and PAUSED retain their chips,
colors, risk marks, filters and precedence. Sports, memecoins, wallet/entity nodes and run-state derivation remain unchanged.
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
