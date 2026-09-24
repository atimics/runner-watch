# Daily market editions

Pre-market is **The opening watch**: scan breadth, the lead stock, Flash's
opening read, the watch board, and saved targets. Post-market is **The closing
story**: one company in focus, a session scorecard, Flash's review, and the
same watch board with its results.

Both editions use the stock board's dark palette, type, risk markers and shared
ring component. Blue marks the morning edition and amber marks the evening.
Green and red keep their existing meaning for moves and outcomes. Patterned
ring slices keep their existing evidence meanings. A dashed ring marks missing
saved detail. The surrounding cover arcs are decorative.

The scanner breadth bar names its scope. Evening breadth comes from the saved
closing scan. Older reports retain their original checkpoints. Each complete
report has a permanent page; the archive shows compact edition covers.

## Company selection

The evening edition scores all closing-scan names with a positive price and a
quote dated to the same Eastern session. The editorial score adds:

- Absolute price change: up to 40 points, reaching the cap at 25%.
- Relative volume above 1×: up to 25 points, reaching the cap at 6×.
- Saved scanner signals: 5 points each, up to 20.
- Saved risk flags: 5 points each, up to 15.

Ticker order resolves ties. Missing fields add zero. Both rising and falling
stocks can lead. The selection is for research interest within scanner coverage.
The report explains this method next to the selected company.

The saved profile contains the selected checkpoint, company name, exchange,
business classification, financial facts and up to four recent filings. Facts
show their financial period and filing date. Public SEC links let readers open
the source. The profile also lists saved signals and risk flags. It uses existing
database evidence and is saved once with the edition. Older editions show their
original watch leader; company profiles begin with newly created evening editions.

## Preview and checks

Run `uv run python scripts/preview_market_editions.py /tmp/report-editions` to
render both production templates with clearly labelled sample data. Serve that
folder locally to inspect the pages. The preview includes fictional companies.

Focused tests cover deterministic selection, price validity, same-session quote
dates, evidence cutoffs, frozen profiles, legacy editions, public routes,
forecasts, commentary and migrations. Visual checks cover desktop, 390px and
320px widths, profile navigation and the selection-method disclosure.
