# Chain-first token discovery

`/memecoins` discovers base tokens from GeckoTerminal's latest DEX pools across
supported networks. Each five-minute collection reads the first page (up to 20
pools). This is a bounded window of the provider's chain index. The product uses
Memecoins as its navigation label; selection covers new tokens based on pool data.

A pool qualifies with a positive USD price, at least $1,000 of liquidity, positive
24-hour pool volume, at least one recorded trade, and a valid pool creation time.
The feed uses chain and contract address for identity. EVM address case is
normalized, while case-sensitive addresses retain their spelling. Multiple pools
for one token resolve to the pool with the greatest liquidity, then volume.
Price and volume both come from that selected pool. Default ordering is pool
volume. Metadata and marketing fields have zero weight in discovery and ordering.
The UI uses chain and address labels.

The keyless source is `geckoterminal/memecoins`. Source runs retain the request
URL and normalized pool records. Records include chain, token address, pool
address, creation time, liquidity, buys, sells, price, volume and daily change.
Market cap stays unknown; FDV has its own field. Quote time records the indexer
fetch time, identified by `time_basis=indexer_fetch`. Pool creation time is stored
separately. The indexer controls its data coverage and update delay.

A database claim shares the request budget across processes and restarts. Saved
quotes become stale after 15 minutes. A failed request preserves the prior
snapshot and timestamps. A successful empty window clears the current discovery
list. A token's saved detail, price history and Calls remain accessible after it
leaves the window. Its quote ages until its pool appears in another collection.
Existing category-era assets retain their IDs, source links, history and Calls.
New chain-address IDs keep the two sets of records separate.

`/api/memecoins` serves the saved snapshot with search and sort parameters.
`MEMECOINS_ENABLED=false` pauses collection. Source attribution links to
GeckoTerminal; the source catalog retains its terms-review status.

Provider references:

- [GeckoTerminal API endpoints](https://api.geckoterminal.com/docs/index.html)
- [Pool liquidity and market-cap field meanings](https://apiguide.geckoterminal.com/faq)
- [API terms](https://www.coingecko.com/en/api_terms)

Validation: `uv run pytest tests/test_chain_discovery.py tests/test_memecoins.py tests/test_memecoin_calls.py -q -o addopts=`.
