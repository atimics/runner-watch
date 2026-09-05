# Memecoin markets

`/memecoins` shows up to 100 coins from CoinGecko's `meme-token` category,
selected by market cap. The page supports name and symbol search, plus sorting
by 24-hour volume, market cap, gainers, and losers. `/api/memecoins` serves the
same saved snapshot with `q` and `sort` parameters.

The `memecoins` worker collects public USD market data every five minutes.
A database claim shares that request budget across processes and restarts.
The keyless endpoint uses the category, currency, page size, and sort order.
Each coin keeps its CoinGecko ID, source link, source time, price, daily change,
volume, and market cap. Source runs appear in the ingestion log. The latest
normalized snapshot lives in `worker_state` under `memecoins_snapshot`.

Quotes become stale after 15 minutes. Missing source times also mark a quote
stale. A failed refresh keeps the saved snapshot and its original collection
time. Prices below one cent retain their precision. Missing values show a dash.
Set `MEMECOINS_ENABLED=false` to pause collection and display the paused state.
The worker remains in the process health check while paused.

CoinGecko attribution appears on the page and each coin links to its source.
The source catalog tracks the terms review as `review_required`, consistent
with other public market feeds.

Provider references:

- [Market endpoint and response fields](https://docs.coingecko.com/reference/coins-markets)
- [Keyless public API](https://docs.coingecko.com/docs/keyless-public-api)
- [API terms](https://www.coingecko.com/en/api_terms)

Validation: `uv run pytest tests/test_memecoins.py -q -o addopts=`.
