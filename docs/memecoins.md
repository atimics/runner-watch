# Helius discovery and on-chain watch

Memecoin discovery starts with finalized Solana transactions returned by Helius.
The first supported protocol is PumpSwap. The collector decodes `create_pool`
instructions, including calls inside other instructions, against the official
PumpSwap interface. Each candidate keeps its mint, pool, creation slot, creation
time, transaction signature, pool creator and declared coin creator.

`getTransactionsForAddress` reads the latest successful PumpSwap transactions
from the past day. Each five-minute run requests up to two pages of 100 full
transactions. `HELIUS_DISCOVERY_TX_LIMIT` sets the page size from 1 to 1,000.
Coverage is sampled. The source receipt records the transaction count and whether
more pages were available. Default usage is at most 576 RPC requests per day;
Helius documents a 10-credit minimum per full-transaction response, so two full
100-transaction pages per cycle would use 5,760 credits per day. Larger page sizes
increase the credit budget. Configure the limit to match the paid plan.

Set `HELIUS_API_KEY` as a secret on the worker. Provider URLs and errors stored in
source receipts keep the key private. A missing key or failed RPC preserves saved
quotes and records a collection error. `MEMECOINS_ENABLED=false` pauses collection.

The collector retains up to 100 discovered pools for one day. It requests USD
quotes from GeckoTerminal for those exact pool addresses, in batches of 30. A
returned pool must match a Helius-discovered address and base mint. Metadata,
categories, promotion and reported market cap have zero weight. Active quote
rows require $1,000 liquidity, recorded trades, positive volume and price. The
quote time is the indexer fetch time; the pool-creation receipt uses Solana block
time. Market cap stays unknown, and FDV has its own field. Quotes age after 15
minutes. Existing assets, Calls and price history retain their IDs and sources.

## On-chain watch

The public memecoin page and `/api/memecoins` include a transaction evidence feed.
A first detector finds PumpSwap buy/sell instructions whose trading wallet was
named in a known pool's creation. It checks the program, pool, mint, successful
transaction status, slot and direction of the wallet's token-balance change.
Each observation keeps:

- Trade signature, slot, block time and transaction link.
- Wallet, mint, pool and observed wallet role.
- Exact net token change across that transaction.
- The pool-creation transaction establishing the wallet relationship.

The two wallet roles are distinct: the pool creator signed the pool creation;
the declared coin creator was named in its instruction data. A declared role is
a claim stored on chain. Pool creation can also involve program-controlled wallets.
These observations establish a transaction relationship. Insider ownership,
intent and real-world identity require further evidence.

The feed retains up to 1,000 unique observations for one day. It works while USD
quotes are pending. Duplicate delivery produces one observation per transaction,
pool, wallet and direction. Last-check time and sampled coverage remain visible.
A gap in the sampled transaction window leaves activity outside the observed set
unknown. Saved observations survive collection failures.

## Further detectors

The same receipt model can support linked-wallet funding, early accumulation,
creator-linked transfers and liquidity withdrawals. Each detector needs its own
protocol checks, wallet relationship evidence and coverage record. A shared funder
is a useful lead; exchanges and shared services require special treatment. A
future case view should separate directly observed facts, inferred wallet groups,
and open questions.

## References and validation

- [Helius transaction-history RPC and metering](https://www.helius.dev/docs/rpc/gettransactionsforaddress)
- [Official PumpSwap interface](https://github.com/pump-fun/pump-public-docs/blob/main/idl/pump_amm.json)
- [GeckoTerminal pool quotes](https://api.geckoterminal.com/docs/index.html)
- [Helius terms](https://www.helius.dev/terms)

Run `uv run pytest tests/test_helius_discovery.py tests/test_chain_discovery.py tests/test_memecoins.py tests/test_memecoin_calls.py -q -o addopts=` and the memecoin browser suite.
