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

Each five-minute cycle reads two pages and one wallet page, each with a
limit of 100 full transactions. Every third cycle the second page reads the
graduation stream instead of a program: the history of Pump's migration
authority (`39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg`, the Global
account's `withdraw_authority`), which takes part in every graduation to
PumpSwap. About a thousand graduations a day fit in a page every 15 minutes,
where sampling the programs caught about eleven. Graduated pools that traded
last cycle keep up to 60 of the 100 pool slots, busiest first, so a runner is
not pushed out by newer graduations. Under Helius's current documented metering, that
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

### Launches, creator selling and launch bundles

Every third full-sampling cycle a page reads the newest 100 launches from
Pump's mint authority (`TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM`, the
PDA of "mint-authority"), which takes part in every launch and nothing else:
about 38,000 a day. It replaces a program sample page, so the credits are the
same, and each read covers the last four minutes instead of a backlog.

`src/runner_web/memecoin_watch.py` watches two risks with one-credit reads,
every cycle including quiet ones:

- **Creator selling.** The creator's token account for each board coin
  (derived locally from the creator and the mint, Token-2022 or the original
  token program) is read in batches of 100. A drop of 1% or more of their
  holding since last cycle is "Creator sold or moved X% of their tokens",
  with the account's newest signature as the receipt. About 2 credits a cycle
  for 200 coins.
- **Launch bundles.** Once per board coin, tagged coins first, up to ten a
  cycle: list the bonding curve's signatures back to the launch (a credit per
  1,000, at most five pages), and when three or more transactions landed in
  the launch slot or the next, read them (a credit each, at most eight). Three
  or more wallets taking 10% or more of supply there is "Launch bundle". Each
  coin is checked once and remembered for a week.

Both are findings the assessment already reads (`creator_sell`,
`synchronized_buys`), so they set AVOID for 24 hours and show their receipts.
A finding on a coin that was SETUP, RUNNING or EXTENDED just before, or that
has an open Call, is posted once to the Telegram channel within its usual
pacing (`TELEGRAM_MEMECOIN_ALERTS`).

### Ratified

A memecoin is Ratified when it meets all nine of RATi's basic standards
(`src/runner_web/memecoin_ratify.py`). It is not an endorsement, a guarantee
or advice.

1. Mint authority revoked.
2. Freeze authority revoked.
3. No risky Token-2022 features (transfer fee or hook, permanent delegate,
   pausing, default-frozen accounts, non-transferable, confidential transfers).
   Pump's metadata extensions are not risky.
4. A graduated pool with $10K or more of real liquidity.
5. At least 24 hours since its pool opened.
6. No launch bundle (remembered for a week), creator selling or liquidity pull.
7. The ten largest holders own 30% or less of supply. Accounts held by a
   known pool or bonding curve (PumpSwap, Pump, Raydium, Orca, Meteora) are
   left out; supply under any other program counts, since a creator can hold
   it under a program of their own (live case: "NPC", 22 holders, once read
   0% by leaving out every program-owned account).
8. At least 100 holders, from GeckoTerminal's token info (free; only for
   coins passing everything else, at most five a cycle, kept six hours).
   "NPC" had 22.
9. Pool liquidity burned or locked: 10% or less of the pool's liquidity
   tokens still exist. The pool's own count of issued tokens is compared with
   the liquidity-token mint's supply (two reads, a credit per 100). Pump burns
   all of a graduated PumpSwap pool's. PumpSwap and Raydium CPMM are read;
   other DEXes, and liquidity held in a lock program rather than burned, show
   as not checked yet, so the coin is not ratified.

Standards 4-6 cost nothing. Only coins passing them are read: the mint account
(one credit per 100, kept once clean, since a revoked authority cannot return
and extensions are fixed at creation) and the largest holders (one credit a
coin, every six hours, at most 20 a cycle). The board shows a Ratified mark on
current quotes; the coin page lists every standard as met, not met or not yet
checked.

### Quiet when unread

Paid reads follow readers. A GET of a memecoin page, `/api/memecoins` or a
coin's live refresh records a view (each web process at most once a minute;
share cards and replay GIFs, which Telegram fetches for our own posts, do not
count). After 30 minutes without one the worker is quiet: it skips the program
and wallet sample pages (10 credits each, most of the spend) and keeps the
graduation stream every 15 minutes and the chain prices every cycle, the
history early detection needs. The next view resumes full sampling. A page
with fewer than 100 transactions ends its window even when Helius sends a
next-page token, because following it returned an empty page for 10 credits.

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

Launches are also quoted before they graduate. A Pump launch trades on its
bonding curve, which GeckoTerminal indexes as a `pump-fun` pool at the curve's
address. Each cycle quotes up to 90 curves from launches seen in the last 24
hours: up to 60 that traded last cycle, busiest first, then the newest. A
launch's curve stops being quoted once its PumpSwap pool is seen. Curve rows
carry `venue: bonding_curve`. Bonding-curve buys and sells are decoded as swaps
(official Pump IDL: account 2 mint, 3 bonding curve, 6 user), so staged buying
and creator selling are found before graduation too.

A contract address someone searches for that we do not track is queued (up
to 20, newest first, kept 7 days). The web request stores only the address.
Each cycle one GeckoTerminal token lookup finds the busiest pool of every
queued address, and those pools are quoted with the other extras. Such rows
carry `discovery_source: "Searched by address"`.

### Chain prices

`MEMECOIN_PRICE_SOURCE` picks where board prices come from:

- `gecko`: GeckoTerminal for every pool, as before.
- `shadow` (default): GeckoTerminal prices the board, and each cycle also reads
  prices from the chain and records how far they sit from GeckoTerminal's in
  `/api/memecoins` → `price_check` (median and 90th-percentile gap).
- `chain`: PumpSwap pools and Pump bonding curves are priced from their own
  accounts through Helius (`src/runner_web/memecoin_chain_prices.py`), about
  three `getMultipleAccounts` calls a cycle at one credit each. GeckoTerminal
  then only prices pools the chain reader cannot and brings activity windows
  (volume, buyers, sellers) for a 30-coin shortlist: half the pools with no
  price yet, half the biggest movers since last cycle. Price changes it did not
  window come from our saved history. If the chain read fails, the cycle
  quotes everything from GeckoTerminal.

Chain liquidity is twice the real quote side of the pool; it leaves out the
virtual reserve a graduated pool carries over from its curve, so it reads lower
than GeckoTerminal's for small pools. A bonding curve is shown once $100 has
gone into it.

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

The collector also saves a versioned sentiment, attention and risk assessment
with each quoted token. See [Model policy and research basis](research/memecoin-sentiment-attention-risk.md)
for its sampled wallet reading, activity weights, source windows and open checks.

## References

- [Helius transaction history and metering](https://www.helius.dev/docs/rpc/gettransactionsforaddress)
- [Official Pump interfaces](https://github.com/pump-fun/pump-public-docs/tree/main/idl)
- [Official Raydium CPMM interface](https://github.com/raydium-io/raydium-idl/blob/master/raydium_cpmm/raydium_cp_swap.json)
- [GeckoTerminal pool quotes](https://api.geckoterminal.com/docs/index.html)
- [Helius terms](https://www.helius.dev/terms)

Validation includes database migration, concurrent credit reservations, page
replay, failed-page recovery, protocol decoding, pattern thresholds, saved Calls,
receipt routes and mobile browser tests.
