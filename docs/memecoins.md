# Solana discovery and on-chain watch

The worker collects finalized Solana transactions through Helius. It follows
Pump launches, PumpSwap pools and trades, and Raydium CPMM pools and trades.
Discovery uses program instructions, account addresses and token balances.
Metadata, marketing and category labels have zero weight.

## Budget and collection

`HELIUS_API_KEY` is a worker secret. The shared daily budget is capped at
**10,000 Helius credits per UTC day**. `HELIUS_DAILY_CREDITS` can lower that limit.
The database reserves the maximum documented request cost before each request.
Failed requests retain their reservation as a conservative allowance. The cap
survives restarts and applies across workers running this ingestion pipeline.
Other applications using the same Helius account have their own usage.

Each five-minute cycle reads two program pages and one wallet page, each with a
limit of 100 full transactions. Under Helius's current documented metering, that
uses at most 30 credits per cycle, or 8,640 credits per day. The program pages
rotate across the three supported programs. Wallet pages rotate across observed
creators and buyers. `MEMECOINS_ENABLED=false` pauses collection.

Each stream keeps a fixed time window and a pagination cursor. A page commits
its transaction receipts, decoded events and next cursor in one transaction.
Failed pages retain the previous cursor. Program streams start five minutes
before their first finalized window; wallet streams start with the previous day.
Later cycles continue forward. The saved progress includes completed time,
backlog seconds, page counts, excluded records and last-check time. The budget
can leave a backlog on busy programs. When a program backlog exceeds one hour,
the collector records the skipped interval and resumes a recent five-minute
window. Gap records are committed with the new page and remain available for
30 days. Wallet streams keep their existing cursor. Coverage begins at the
recorded windows and includes these explicit gap records.

## Stored evidence

Migration 58 adds transaction receipts, decoded events, stream progress and daily
credit reservations. Receipts contain the transaction fields used by the parsers
and a SHA-256 content hash. Events keep the signature and instruction path.
Duplicate pages preserve one receipt and one copy of each event.

Retention is up to 30 days or 250,000 transaction receipts, whichever limit is
reached first. Events expire with their transaction receipts. A bounded analysis
pass reads the latest 5,000 events plus up to 1,000 pool-creation and 1,000 launch
events for relationship context. Its observed time window is returned with results.

`/api/memecoins/evidence/{signature}` returns the stored transaction receipt and
hash. `/api/memecoins` returns market rows, findings, wallet funding links,
observed buyer/seller counts, analysis times, ingestion progress and credit usage.

## USD quotes and saved Calls

Helius supplies candidate pool addresses. The worker requests GeckoTerminal USD
quotes for up to 100 of those pools in batches of 30. A returned pool must match
a discovered address and base mint. The quote feed can enrich existing candidates.

Quote rows require positive price and volume, recorded trades and at least $1,000
pool liquidity. Price and volume come from the same representative pool. Quote
time records the indexer fetch; creation and trade receipts use Solana block time.
Market cap stays unknown, while FDV has its own field. Quotes age after 15 minutes.
Saved assets, Calls and price history retain their IDs and original sources.
Findings can appear before a USD quote is available.

## Analytics

See [Forensic methods and evidence rules](memecoin-forensics.md) for the detectors
and their interpretation. The public page links findings to their supporting
transactions and labels observations, relationships and patterns separately.

## References

- [Helius transaction history and metering](https://www.helius.dev/docs/rpc/gettransactionsforaddress)
- [Official Pump interfaces](https://github.com/pump-fun/pump-public-docs/tree/main/idl)
- [Official Raydium CPMM interface](https://github.com/raydium-io/raydium-idl/blob/master/raydium_cpmm/raydium_cp_swap.json)
- [GeckoTerminal pool quotes](https://api.geckoterminal.com/docs/index.html)
- [Helius terms](https://www.helius.dev/terms)

Validation includes database migration, concurrent credit reservations, page
replay, failed-page recovery, protocol decoding, pattern thresholds, saved Calls,
receipt routes and mobile browser tests.
