# Ticker maps

Stock and coin pages place their own map beside a source panel. The market Map view is a gallery of ticker links. Sports entries open their game page.

## Stock evidence

Migration 69 adds `sec_filings.evidence_json` and a ticker/history index. The collector saves every Form 4 transaction line, including derivative rows, reporting people and their SEC CIKs, share class, trade date, acquired/disposed code, price, units, ending shares, and referenced footnotes. Purchase, sale, grant, exercise, gift, and tax-payment codes have separate labels.

Each joint transaction remains one event linked to its reporting people. Schedule 13D/G rows retain each person's reported share class, shares, percentage, and as-of date. Shared interests remain separate. Amendments retain their own accession and filing date.

`GET /api/stocks/{ticker}/map` reads up to 50 filings from that ticker per page. Its cursor orders tied dates by accession. Coverage reports the saved filing count. The timeline uses filing time. Selecting an event also marks its filing time on the saved price chart when that date is covered.

People use SEC CIKs where available. Name-based matches carry a visible label. Bubble sizes are equal. Older aggregate rows carry “Filing summary.” Each collector cycle attempts to restore 20 older rows from saved SEC XML documents. The restore cursor lives in `worker_state.stock_map_restore_after`; deleting that key starts another archive pass.

The Schedule parser accepts both SC and SCHEDULE form names. Its cover-page mappings follow the SEC [XML specification](https://www.sec.gov/file/schedule-13d-13g-tech-specs-20), including the separate Schedule 13G person fields. Missing numeric values remain empty, and numeric zero stays zero.

## Coin evidence

The coin map opens at the final saved frame. Launch, replay, timeline, saved revision links, GIF export, and evidence packages keep their existing receipt data. Wallets and recorded events open the visible source panel. The activity list follows the chosen frame.

## Checks

`tests/test_stock_map.py` covers transaction lines, joint filers, ownership rows, modern form names, archive restoration, stable identities, source links, legacy summaries, and ticker pagination. `tests/test_browser_stock_map.py` covers mobile layout, keyboard use, shared selection, time filtering, older pages, source recovery, and safe text rendering. Coin and shared screen tests cover their existing flows.
