# Mobile stock list sparklines

At widths up to 760 CSS pixels, the stock list replaces its left-hand status chip
with a 64 × 22 pixel sparkline. The exact server-provided status appears beneath
it in the existing palette: WATCH purple, SETUP blue, RUNNING green, EXTENDED
orange, AVOID red, PAUSED gray. Risk triangles are retained. There is no bell or
new alert action. Status derivation, filter counts, glyphs and verification rules
are unchanged. Desktop keeps its chip, 64 × 18 chart column, and navigation arrow.
Other markets do not use the mobile stock layout.

The chart and text communicate different things. The line is green or red for
an increase or decrease from its first saved price, or gray for unchanged prices.
This does not recolor the status, and is not necessarily the session percentage
shown beside the latest quote. The accessible description and SVG title include
the saved time range, prices, and change. All rows retain the existing shared
symmetric percentage domain (minimum ±2%), so the same vertical distance means
the same percentage move within the batch. Time, not sample index, sets x position.

Only valid positive saved prices are drawn. Timestamps are sorted and duplicate
times keep the last valid observation. No history is an em dash, one observation
is a point, and a flat line requires multiple equal observed prices. There is no
invented placeholder trend or extrapolated candle. Pulse-entry markers are kept
only within the observed range. The sparkline is a compact overview, not a
substitute for the ticker's full price history and freshness information.

## Loading and refresh

Reuse `/api/pulse/charts`, now covering the full bounded 50-candidate board rather
than only the first 20. The startup chart warm uses the same batch; detail warming
still covers only five stocks. No per-row endpoints, dependencies, schema change,
or new periodic timer are added. This increases maximum cold batch work; it is
not a latency optimization. Existing chart caching and provider policy remain.

The existing accepted 60-second list refresh repaints saved charts after replacing
the markup, then fetches one chart batch. In-flight requests coalesce. Failed or
malformed responses keep a previously drawn chart, dim it, and label it as saved
history whose refresh is unavailable. The 25-second request timeout releases the
loader for a later retry. An explicit successful response with missing/empty data
clears the old series instead of preserving fictitious availability. Every valid
batch replaces the cache (up to 50 entries), so departed tickers cannot distort
the scale indefinitely. Chart refresh does not award verification or change state.

Regression coverage includes mobile/desktop breakpoints, unchanged state palette
and vocabulary, exact tag filtering, glyph/check preservation and revocation,
up/down/flat/missing/one-point samples, timestamp spacing, failed refresh recovery,
new rows, removal of old scale outliers, and one request per batch.
