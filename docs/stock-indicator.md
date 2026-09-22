# Stock attention glyph

Presentation-only change for the stock list and detail screen. Scoring, model
probabilities, ordering, policy, and the existing `state_tag()` derivation are
unchanged. WATCH, SETUP, RUNNING, EXTENDED, AVOID and PAUSED retain their chips,
colors, risk marks, filters and precedence. Sports, memecoins and map-node
renderers are not redesigned by this change.

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
event to update all glyph fields, text and check. A missing updated indicator
clears verification rather than preserving stale clearance. There is no extra
poller or database query.

## Accessibility and verification

The glyph and approval mark have accessible names. The list has a keyboard/tap
accessible indicator key, and following the existing stock link opens a visible
text breakdown. A tooltip is supplementary, never the sole explanation. Details
remain server-rendered with JavaScript disabled. Client refresh uses DOM APIs,
fixed color keys and textContent, not untrusted HTML. Desktop and mobile list
layout, risk and sentiment independence, true solid fill, check revocation and
unchanged status/filter behavior have regression coverage.

No schema migration, dependency, scoring-threshold or production setting changes.
