# Stock scanner controls

The desktop Scan page uses the connected local scanner. Choose a universe, a price
range, and minimum daily liquidity, then select **Run scanner**. Custom ticker
lists accept up to 500 symbols. Spaces, commas, and semicolons separate symbols;
share classes such as `BRK.B` become `BRK-B`.

**More options** holds the intraday scan limit, the result limit, and the existing
60% drawdown filter. Reset restores the starter settings for the form. A completed
receipt keeps its settings, and reopening Scan restores the latest saved run.
Automatic refresh also uses the latest run's settings.

## What the filters mean

- Price is the previous completed session's close.
- Daily shares are the median volume of up to 20 completed sessions.
- Daily value is that volume multiplied by the previous close.
- Relative volume compares cumulative volume at the quote time with earlier
  sessions in the available intraday history.
- Price, liquidity, and drawdown filters apply before the intraday scan limit.
- Rank applies to all usable intraday results, before the result limit.

The table shows each source quote time. The completed time describes the scan run.
Market data can be delayed or incomplete.

## Scan receipt

`POST /api/v1/scans` accepts the existing `ScanRequest` fields plus `sort`:
`score`, `volume`, `gainers`, `losers`, `price_asc`, or `price_desc`.
The default remains setup score. Custom symbols are normalized and validated at
the API boundary. Numeric bounds must be finite.

The response stores `request` with the exact accepted settings, alongside:

| Field | Meaning |
| --- | --- |
| `requested_symbols` | Distinct universe symbols sent for daily data |
| `liquid_symbols` | Usable daily profiles that passed the filters |
| `scanned_symbols` | Symbols with returned intraday frames |
| `matched_symbols` | Usable analyzed results before the display limit |
| `rows` | Ranked results up to `top_n` |
| `failed_symbols` | Symbols reported as failed by the data provider |
| `scan_cap_reached` | Daily matches exceeded `max_symbols` |
| `result_cap_reached` | Usable results exceeded `top_n` |

The client reads older receipts with their existing fields. The table uses a dash
for missing coverage counts. Zero remains zero.

## Next web step

The public stock Radar currently presents saved Pulse events. Its next scanner
view needs a saved-universe query with filters before pagination. The local Scan
request and receipt above define the shared filter meanings and coverage labels.
