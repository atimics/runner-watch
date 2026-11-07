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
the time limit, or an operator exit request also triggers an exit. Paper mode
queues the exit. Live mode saves a proposal for the operator to review. Price gaps,
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

## Dash's private trading desk

Set `DASH_TRADING_TELEGRAM_IDS` to the owner's numeric Telegram user ID. Set
`TRADING_OPERATOR_IDS` to that owner's application user ID. The web process
needs the public `TRADING_WALLET`, `TRADING_RPC_URL` and `JUPITER_API_KEY` for
quote previews. The separate live process owns the signer file.

In the owner's private chat, Dash can use `trading_status` and `propose_trade`.
He names the full Solana token address, a buy amount in USDC or a full-position
sale, his thesis, and what would change his view. The proposal returns the
private `/trading?mode=live` review link. A repeated Telegram update returns the
same saved proposal. Public Calls continue to use Flash.

The owner chooses **Review trade** to see a fresh quote, exact input, minimum
output, maximum SOL fees, wallet, balance share and reasoning. **Approve trade**
requires a recent passkey login and the current review token. Approval reserves
the order once. Its limits last two minutes. Each purchase and sale needs its
own approval, including exits from stop, profit and holding-time rules.

The worker rechecks market evidence and quotes before signing. The token,
amount, wallet, minimum output, fee cap, policy and approval deadline must match
the saved review. A changed price or expired approval closes the queued order;
Dash can create a new proposal for review. A signed order keeps its saved bytes
through finality checks and recovery.

### Create Dash's wallet

The owner runs the setup command on the dedicated live host:

```sh
uv run stonks-trading-wallet /data/dash-wallet/signer.json
```

It creates a new Solana signer in a private directory and prints the public
funding address. The signer file has mode `0600`. Existing files are preserved.
Store a backup through the host's secret storage. Put the printed public address
in `TRADING_WALLET` and the file path in `TRADING_KEY_FILE`. Send USDC for the
trading budget and SOL for network fees. Keep these settings on the dedicated
live process and share its database with the web process.

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
TRADING_POLICY_JSON=<reviewed risk and fee limits>
JUPITER_API_KEY=<provider key>
```

The live worker uses the shared database and its own account lease. A new live
account starts at zero. Each cycle reads finalized balances. USDC and SOL
deposits enter the funding ledger once. USDC deposits increase the spending
budget and adjust the loss anchors by the same amount. SOL pays network fees.
A withdrawal or a change to a held token balance pauses entries for review.
The account binds to its wallet on its first balance check.

For live orders, available funded USDC is the entry and exposure budget. The
operator approves the amount of each order. Pool-size checks, position limits,
price impact, fees and loss checks still apply. The initial capital, fixed entry
size and fixed exposure fields describe the paper strategy.

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
  tests/test_trading_routes.py tests/test_trading_holders.py tests/test_dash_trading.py
uv run pytest -o addopts= -m browser tests/test_browser_trading.py
```

Tests cover reservations, restart recovery, one settlement per order, missing
exit routes, stale evidence, holder aggregation, transaction validation,
provider timeouts, expiry, finalized failures, operator access, and the desk at
320, 390, and 1,440 pixels. Migration 106 supports SQLite and PostgreSQL.

Provider contracts: [Jupiter order and execute](https://developers.jup.ag/docs/swap/order-and-execute),
[Jupiter v6 CPI interface](https://github.com/jup-ag/jupiter-cpi/blob/main/idl.json),
and [Solana transaction confirmation](https://solana.com/developers/cookbook/transactions/confirmation).
