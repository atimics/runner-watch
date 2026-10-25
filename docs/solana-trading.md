# Solana trading

The trading desk is at `/trading`. The `stonks-trader` process owns one paper
account. A separately configured process can own one live account. Each account
has its own policy, cash, orders, positions, fees, and outcome records.

## First strategy

`solana-setup-v1` watches fresh SETUP transitions on graduated Solana pools.
Entries need all nine RATi checks, fresh price and chain evidence, at least three
sampled recent swaps, clean mint controls, a dated holder check under rules 7,
and a dated liquidity lock check. The existing pool-age rule is 24 hours.
Holder concentration combines accounts owned by the same wallet. It remains an
estimate from the largest token accounts; the full ownership graph can differ.

| Setting | Default |
| --- | --- |
| Starting USDC | 1,000 |
| Starting SOL fee reserve | 0.05 |
| Entry | 25 USDC |
| Open positions | 4 |
| Total entry exposure | 100 USDC |
| Real pool liquidity | At least 25,000 USD |
| Entry share of pool | At most 0.5% |
| Entry price impact | At most 1% |
| Full round trip cost, including fees | At most 5% |
| Stop / profit target | 15% / 30% |
| Holding time | 6 hours |
| Daily loss / peak drawdown | 30 USDC / 10% |
| Worker cycle | 30 seconds after each cycle finishes |

Stops use a fresh quote for the whole position. AVOID, a chain risk finding,
the time limit, or an operator exit request also queues an exit. Price gaps,
provider delays, congestion, and lost liquidity can change the realized loss.
An unavailable route leaves the quantity open and schedules another attempt.
Pausing entries preserves exit monitoring and transaction recovery.

The market collector keeps sampling while an active trader heartbeat is fresh.
Existing Helius budgets still apply. Jupiter requests use a separate paced
client. Open trading positions receive their own exit quotes even when the
token leaves the board or every browser is closed.

## Paper account and results

Paper cash starts at 1,000 virtual USDC. The separate 0.05 virtual SOL reserve
pays estimated network and token-account costs. An entry reserves USDC before
it can fill. Token quantities use raw integer units.

A paper entry waits at least three seconds. A later worker cycle requests a new
size-specific quote. Its fill uses the quote's slippage minimum and a further
0.5% deduction. Entry and exit checks include a priced SOL fee estimate. Entry
costs reserve at least 2,039,280 lamports for a token account plus 100,000
lamports for network cost. A separate output fee, when supplied, is also
reserved. The cost model is deliberately conservative and is versioned with
the account. It measures an assumed fill model; live chain execution determines
real fills.

Account value is USDC cash plus fresh net exit quotes minus accumulated fees.
SOL inventory is displayed separately. An incomplete exit valuation shows
`Awaiting quote`. The ledger distinguishes cash, reserved cash, token quantity,
fees, and realized position results. A unique order settlement makes retries
repeat-safe.

Every observed SETUP transition gets a decision record, including skipped
entries. A baseline quote is saved within the per-cycle budget, and an outcome
check is due six hours later. Failed quotes and late checks retain their reason
and time. These records survive board and quote-history pruning. Outcome tables
describe coverage; strategy results need a representative paper observation
period before capital sizing decisions.

## Running paper mode

Run migrations, then start the dedicated process:

```sh
uv run stonks-migrate
TRADING_MODE=paper uv run stonks-trader
```

The Fly process group `trader` explicitly selects paper mode. `/api/trading`
serves the paper account. `/health/trading` reports its heartbeat and health.
The release check requires a recent heartbeat. Provider and quote failures
remain visible in the desk and health response.

Set `JUPITER_API_KEY` for the provider's authenticated allowance. The client also
supports public quotes and spaces those requests by at least 2.1 seconds.
Choose a dedicated provider plan for the live process through `TRADING_RPC_URL`.

`TRADING_POLICY_JSON` accepts the integer fields of `runner_trading.config.Policy`.
USDC fields use millionths of USDC; SOL fields use lamports; percentages use
basis points. On creation, the account stores the complete policy and its hash.
Subsequent starts require the same policy. Changing strategy or capital requires
an explicit account migration with ledger evidence.

## Operator controls

Set `TRADING_OPERATOR_IDS` to the comma-separated application user IDs allowed
to manage the desk. These people can pause or resume entries and request a full
position exit. Changes require the normal same-origin check, passkey login,
and recent authentication. Requests are recorded in the event journal.
Live pages and APIs also require operator access. Signed transaction bytes stay
inside the execution database.

## Running live mode

Use a dedicated process and an operator-funded wallet. Keep the signer file on
that process, owned by its operating-system user, with mode `0600`. The file is
a Solana CLI JSON keypair. Configure:

```text
TRADING_MODE=live
TRADING_LIVE_ENABLED=1
TRADING_WALLET=<dedicated public key>
TRADING_KEY_FILE=<absolute secret file path>
TRADING_RPC_URL=<dedicated HTTPS Solana RPC>
TRADING_POLICY_JSON=<policy matching the funded balances>
JUPITER_API_KEY=<provider key>
```

The live worker uses the shared database and its own account lease. Its initial
USDC and SOL balances must match the policy exactly. Each cycle compares saved
cash and held token quantities with finalized chain balances. A difference
pauses entries for operator review. Keep account reconciliation in the ledger
when changing the wallet's funding.

The first live adapter accepts plain SPL mints and reviewed Jupiter v6 `route`
and `shared_accounts_route` instructions. It parses known swap variants 0–38
from Jupiter's published CPI interface. A new layout or swap variant records a
clear eligibility error. The validator checks the sole signer, fee payer,
token accounts, exact input, output minimum, mint controls, fee cap, address
tables, and simulation balance changes before signing. Supported routes can
therefore be a smaller set than Jupiter's full route catalogue.

A transaction's signed bytes, signature, quote, and block-height expiry are
saved together before submission. After a lost reply or restart, the worker
checks that same signature and can submit the same bytes again. Finalized
transaction balance changes settle the ledger. A finalized block height beyond
expiry and an absent historical signature release an unlanded reservation.
Unexpected finalized balances preserve the order for operator review.

The tests use generated test keys and mocked chain responses. Live capital
qualification requires a controlled funded run and inspection of its actual
transaction receipts. This release's deployment configuration starts paper mode.

## Verification

```sh
uv run pytest tests/test_trading.py tests/test_trading_live.py \
  tests/test_trading_routes.py tests/test_trading_holders.py
uv run pytest -o addopts= -m browser tests/test_browser_trading.py
```

Tests cover reservations, restart recovery, one settlement per order, missing
exit routes, stale evidence, holder aggregation, transaction validation,
provider timeouts, expiry, finalized failures, operator access, and the desk at
320, 390, and 1,440 pixels. Migration 105 supports SQLite and PostgreSQL.

Provider contracts: [Jupiter order and execute](https://developers.jup.ag/docs/swap/order-and-execute),
[Jupiter v6 CPI interface](https://github.com/jup-ag/jupiter-cpi/blob/main/idl.json),
and [Solana transaction confirmation](https://solana.com/developers/cookbook/transactions/confirmation).
