# Memecoin sentiment, attention and risk

Policy: `memecoin-sar-v1`. The collector saves one assessment with each quoted
token. Its three readings use the same collection time and retain their own
meaning. The list, token detail and API read the saved result.

| Reading | Question | Current evidence |
|---|---|---|
| Attention | What deserves investigation? | Absolute price movement, reported volume, sampled wallet activity and material chain events |
| Sentiment | Which way did sampled wallets trade? | Net token changes across observed swaps over 15 minutes |
| Risk | Which concerns have supporting receipts? | Creator-linked selling, pool withdrawals and recorded wallet patterns |

These are descriptive readings. Attention weights are explicit policy choices
awaiting outcome testing. Sentiment percentages describe sampled addresses.
Risk names observations and patterns with their original receipts and context.

## Attention policy

Market activity contributes up to 50 points:

- Absolute reported 24-hour price change, capped at 35 points.
- `5 * log10(1 + reported_24h_volume_usd / 1000)`, capped at 15 points.

Chain activity contributes up to 50 points:

- `5 * log2(1 + filtered_directional_wallets)`, capped at 25 points.
- The largest material-event contribution in the previous 15 minutes: creator
  buy or SOL transfer 5; common funding, synchronized buying or repeated rounds
  10; creator sale 15; liquidity withdrawal 25.

The components sum to the attention reading. Direction changes preserve the
absolute market contribution. Material risk events also deserve attention.
Each event retains its observation, relationship or pattern label.

Quote fields require a positive price and a source timestamp within 15 minutes,
with the existing 60-second allowance for quote clock skew. Chain activity
requires a collection timestamp within 15 minutes and events at or before the
assessment time. Missing inputs have an explicit unknown contribution. External
social contribution remains unknown. Market cap and pool reserve estimates have
zero scoring weight in this version.

## Sampled direction

For each token, deduplicate swaps by transaction signature, wallet and direction.
Sum signed token changes per wallet in the last 15 minutes. Each remaining
wallet contributes one net-buying, net-selling or balanced reading. The displayed
split uses directional wallets; balanced and excluded counts appear in the
description. Receipts measure token changes across swaps in the sample.

Known launch-linked wallets and wallets in recorded common-funding,
synchronized-buy or repeated-round patterns are excluded from this reading.
Those exclusions describe a conservative research filter. Wallet addresses and
reasons remain in the saved result. Shared services, arbitrage and market making
remain possible explanations for the patterns.

The user label is **Observed swap balance**. The saved compatibility fields
`chain_sentiment_counts.bullish` and `.bearish` hold net-buying and net-selling
wallet counts. The display spells out these units and the 15-minute window.

## Risk and coverage

Risk factors cover the previous 24 hours. Each factor must match the token and
retain source receipts. Token control powers, holder concentration, executable
sale proceeds, external sentiment and paid promotion remain checks awaiting data.

Collection is sampled: the worker rotates bounded program and wallet pages, and
the forensic pass has global event and finding limits. Every model receipt keeps
partial coverage, quote inputs, source times, program backlogs and recorded gap
counts. A factor-free sample leaves overall risk unknown.

After 15 minutes, or when the quote is stale, current attention and direction
readings expire together. Current risk flags expire after 24 hours from their
event time. The original model receipt and its dated findings remain available.
Stock scoring and paper Call policy retain their existing contracts.

## Early reading and tags

Policy: `memecoin-early-v2` (`src/runner_web/memecoin_early.py`). Attention reads
the last 24 hours, so it peaks after a move in either direction. The early
reading compares the pool's last hour with its own pace over the five hours
before it, from GeckoTerminal's 1h and 6h windows, while the price has not yet
moved. It uses the stock tags and colours:

| Tag | Meaning |
|---|---|
| SETUP | Volume or buyers well above their 6h pace, mostly buying, price not yet run |
| RUNNING | Up 50%+ in the hour or 150%+ in six hours |
| EXTENDED | Up 300%+ in six hours or 500%+ in 24 hours |
| AVOID | Pool under $5K, down 90%+ in 24h, down 40%+ this hour, or a creator sale, liquidity withdrawal, synchronized buying or common funder finding |

SETUP points: volume pace and buyer pace (up to 35 each; twice the usual pace
earns a third), buyer share above half (up to 20) and unlinked net buyers in the
chain sample (up to 10). Each pace is the stronger of the last hour against the
five before it and the last five minutes against the hour (which needs $500 and
5 buyers in those five minutes), so a coin under an hour old can still show one. Only time the pool existed
counts as "before": a 5-minute pace needs a pool at least 15 minutes old and an
hourly pace one at least 2 hours old (a seven-minute-old pool once read
98,877× its pace).
A score of 30 with at least one stated reason is a SETUP. An hour under $1,000
or 10 buyers is too small to read. A bonding curve is never AVOID for thinness:
it always quotes and has no pool to drain until it graduates.

### Copied names

Scammers copy the name of a coin that already has attention, as with
"trolloween" copies each Halloween, so a burst of copies points at the original
(`src/runner_web/memecoin_copycats.py`). Names fold case, spacing and look-alike
characters ("Tr0lloween", "TRO11OWEEN"). Three distinct launches taking one
name within 24 hours are a burst. Each launch counts under its name, or its
symbol only when it has no usable name.

The name only finds candidates. For up to three bursts a cycle (cached six
hours, searched after the pools are quoted, so a new original joins the next
cycle), a GeckoTerminal pool search returns coins by that name; the original is
the contract address named the same (a matching symbol alone is not enough)
whose pools are at least seven days older than the burst, hold the most
liquidity in total, at least $5,000, and still trade at least $1,000 a day. An
older copy that died loses to the funded original, and a generic word does not
pull in an unrelated dead coin (live case: "Hold My Glasses", symbol GLASSES,
no volume, once answered a "Glasses" burst). The original is quoted like any pool and earns up
to 30 copycat points, enough for SETUP on a burst of three even while its own
trading is quiet. Every launch in the burst that is not the original is AVOID
("Copies an older coin's name") and its page names the original by address.
Without a verified original, nothing is marked. A failed search skips that name
for the cycle and never fails the refresh.

These weights are a starting heuristic. Every saved quote keeps the features,
score and state in `memecoin_quote_history.features_json`. The test is: for
each state, how often the price doubled within six hours while liquidity held,
against how often it fell by half. Weights change only on that evidence.

## Validation and next measurements

Tests cover worker persistence, list/detail parity, stale readings, source windows,
network and token isolation, duplicate instructions, repeated buys, round trips,
creator and funding exclusions, market-cap independence and preserved risk context.

The next research stage should collect token powers, holder snapshots, paid
promotion receipts and size-specific executable sale quotes. Each needs its own
source time, units and coverage. Test this policy against simple liquidity and
price/volume baselines on a later fixed launch cohort. Follow inactive tokens and
failed exits, and account for fees, delay and price impact.

## Research basis

- [Meme Coin Factories, September 2026](https://arxiv.org/html/2609.10246v1):
  sampled wash trading, creator funding groups and coordinated activity.
- [USENIX Security 2026](https://www.usenix.org/conference/usenixsecurity26/presentation/mongardini):
  manipulation indicators in a measured high-return token subset.
- [Revised graduation-label audit, version 4](https://arxiv.org/abs/2607.02823v4):
  outcome-label quality and validation on a later time period.
- [DEX Screener Boosts](https://docs.dexscreener.com/boosting): paid promotion
  affects discovery placement.
- [Solana extensions](https://solana.com/docs/tokens/extensions): separate token
  control powers requiring direct state evidence.

The papers motivate measurement choices. Their cohort percentages are separate
from policy weights and each token's saved findings.
