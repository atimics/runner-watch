# Memecoin forensic methods

The on-chain watch is an evidence feed for investigating token launches and
wallet behavior. Every finding points to the underlying transactions. It records
observations and review leads, with clear limits on wallet attribution.

## Observed data

The first protocol parsers cover:

- Pump `create` and `create_v2`: mint, launch wallet, declared creator and bonding curve.
- PumpSwap `create_pool`, `buy`, `buy_exact_quote_in`, `sell` and `withdraw`.
- Raydium CPMM pool initialization, swaps and liquidity withdrawal.
- Parsed Solana system transfers: sender, recipient and lamports.

Outer and inner instructions keep distinct paths. Transactions must have succeeded.
Mint and wallet addresses are decoded as 32-byte Solana keys. Token quantities use
integer balances and decimal scaling. A trade quantity is the wallet's net token
change across the transaction. This basis remains explicit where several swaps
occur in one transaction.

## Detector rules

### Creator-linked trading

A wallet named in a known launch or pool-creation event performs a later matching
swap. The program, mint, wallet, successful status and token-change direction must
agree. Evidence includes both the creation event and the trade.

Pool creators and declared coin creators are distinct roles. A declared creator
is a value supplied in an on-chain instruction. Program-controlled accounts can
also take part in creation. The record establishes the observed relationship;
real-world ownership and intent remain open questions.

### Common funding source

Two or more distinct buyers of the same token received SOL from one source before
their observed buys. Each funding transfer and buy becomes part of the finding.
Groups above 25 buyers are left out of this bounded detector. Shared services and
exchange withdrawal systems can produce the same relationship.

The graph keeps individual directed transfer edges. Transitive funding chains
remain inspectable as separate edges. Collapsing wallets into an ownership entity
requires additional evidence and a separate decision.

### Synchronized buying

At least three distinct wallets buy the same token within ten seconds. Their net
token amounts must lie within 1% of the first amount in the window:
`abs(amount - first_amount) <= 0.01 * first_amount`.

The finding is a timing-and-size pattern. It is a lead for reviewing transaction
ordering, funding relationships and repeated behavior.

### Repeated buy/sell rounds

A wallet makes four alternating buys and sells within ten minutes. The aggregate
bought and sold quantities differ by at most 5% of the bought quantity:
`abs(bought - sold) / bought <= 0.05`.

Arbitrage and market making can also produce this pattern. Each supporting swap
is retained. Repeated instructions in one transaction contribute one net-change
observation per wallet, token and direction.

### Liquidity withdrawals

A supported withdrawal instruction and a positive wallet token balance change
produce an observation. It includes the pool, wallet, mint, LP amount in raw
units, net token amount and source transaction.

### SOL movements from launch-linked wallets

An observed wallet named in launch or pool creation sends at least one SOL after
that event. The finding includes the original relationship, transfer, recipient
and lamports. The recipient's real-world identity remains unknown.

## How the broader forensic outline maps to Solana

Common-gas-source analysis maps to observed SOL funding and fee-payer evidence.
Behavioral work uses instruction data, transaction timing, quantities and slot
ordering. Protocol-specific decoders provide the structural evidence needed to
interpret pool actions. EVM `skim()` and `sync()` behavior requires EVM adapters.

Further work can add multi-hop funding paths, repeated launch histories, priority
fee and ordering patterns, bridge messages and verified exchange attribution.
Bridge evidence needs matching source and destination records. Exchange labels
need a dated, reviewable attribution source. Legal identity belongs to records
held by the relevant service or authority.

## Coverage and interpretation

The ingestion API reports stream windows, progress and backlog. The analysis API
reports the number and time range of events evaluated. A quiet finding list is
an empty observed result for that coverage window. It leaves activity outside the
window unknown. Pattern labels carry their supporting transactions and their
alternative explanations.

The first implementation runs six detector families. Further protocols and
cross-chain attribution can extend the same receipt model as their parsers and
source evidence become available.
