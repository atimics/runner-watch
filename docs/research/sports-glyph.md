# Sports sentiment, attention, and risk

Sports uses the stock and memecoin glyph shape. Each game is a ticker. The selected
outcome and its settlement terms set the scope for all three readings. The glyph
stays to the right of the score. The key follows the full list; the explanation
lives inside the game detail.

## Sentiment: RATi's opinion versus market

The outer ring is green when RATi's model values the selected outcome above the
named benchmark, red with stripes when below, and gray when equal at the displayed
0.1 percentage point precision. The exact difference appears beside the outcome
and in the accessible reading. Fresh comparisons use the existing priority:
Kalshi, Polymarket, then sportsbook. Dashed rings mean fresh comparable data is
pending. Thus GB at RATi 59.1% versus market 68.5% has negative sentiment even
though GB is the favorite. Selecting ATL reverses the opinion and labels it ATL.

## Attention: observed activity

The size and fill reuse the shared 0–100 display scale: under 40 is small,
40–69 is medium, and 70–100 has a solid center fill. This is a transparent,
experimental display index. Cross-asset comparisons remain qualitative because
the input sources differ.

`sports-glyph-v1` uses data from the preceding 24 hours:

- Volume: `min(50, 10 * log10(1 + volume_24h))` for each fresh venue; keep the
  largest contribution. Kalshi reports contracts for that outcome market;
  Polymarket reports USD for the whole matched market. The readout names the
  units and scope. Keeping the strongest contribution avoids adding unlike units.
- Price movement: `min(30, 3 * (max_price - min_price) * 100)` per venue; keep
  the largest contribution. Use up to 160 recent saved points. Require a current
  usable quote and at least ten minutes between the earliest and latest usable
  reading. Each observation must match the selected contract and outcome.
- News: four points per unique linked article, capped at 20. Use the eight latest
  saved articles with both publication and collection times at or before the
  reference time. Deduplicate by source URL. News coverage is partial and follows
  the existing event promotion collector.

Volume and movement form the blue market slice. News forms the striped purple
slice. A known zero activity reading has an empty solid inner ring. Missing
activity has a dashed inner ring. The detail names missing inputs.

Team games use the earlier of now and kickoff as their reference time, preserving
saved pregame readings during and after the game. Cup uses the current time.
Fresh means checked within 30 minutes of that reference time. Current activity
uses venue observation time; upstream market metadata update time is also saved.
Quotes with wide or pending spread checks retain their reported volume, while
usable quotes supply price movement and market gaps. Repeated polls have zero
additional weight. External social inputs remain a future data source.

## Risk: limits of the comparison

The center diamond marks model or market limits: experimental model calibration,
saved model input concerns, pending coverage, a spread of at least five percentage
points, or ten percentage points of disagreement among comparable venues. The
center circle marks a closer-check condition: stale model, stale venue quote, or
a wide spread. An unmodeled event shows a question mark. The detail lists the
specific checks. Probability continues to describe outcome uncertainty separately.

Every existing sports baseline is experimental, so modeled events retain a risk
marker even when current market prices are usable. The risk symbol expresses
these checks, rather than a calibrated loss estimate.

## Data and implementation

Migration 90 adds `metadata_json` to team quote receipts. The existing collectors
save 24-hour volume, its units and scope, and spread alongside each observation.
Cup receipts already use JSON and receive the same fields. Earlier receipts keep
unknown volume. This uses the existing collection schedule and public requests.
List and detail views run the same pure adapter, and the shared glyph renderer
keeps stock and memecoin visual behavior intact.

Primary field references, checked September 24, 2026:

- [Kalshi market response](https://docs.kalshi.com/api-reference/market/get-market):
  `volume_24h_fp`, dollar bid and ask, and market update time.
- [Polymarket market response](https://docs.polymarket.com/api-reference/markets/get-market-by-id):
  `volume24hr`, `spread`, and market update time.

Validation covers favorite/value disagreement, outcome and settlement changes,
zero versus missing data, stale and future inputs, invalid volumes, deduplicated
news, repeated quotes, saved activity metadata, list/detail agreement, and mobile
and desktop layout.
