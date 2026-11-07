# Runner Watch developer API

The v1 API sells bounded access to saved Solana analysis and fresh chain checks.
API credits belong to the account. All its keys share the same allowance.
Flash continues to pay for app activity.

## Prices

| Access | Credits | Request limit | Concurrent requests |
| --- | --- | --- | --- |
| Free | 1,000 per UTC month, up to 100 per UTC day | 10/minute | 1 |
| Paid | 1,000 per USDC; $1, $5, and $20 packs | 60/minute | 5 |

Free credits reset each calendar period. Purchased credits carry forward.
Saved reads use available free credits first, then purchased credits.
Fresh jobs spend purchased credits. The paid request limit applies while the
purchased balance is positive. A failed request returns its reserved credits.

| Route | Credits | Bounds |
| --- | --- | --- |
| `GET /api/v1/tokens/{id}` | 1 | One saved token snapshot |
| `GET /api/v1/tokens` | 5 | Up to 50 tokens, ranked within a saved revision |
| `GET /api/v1/tokens/{id}/analysis` | 10 | Token insights, controls, findings, and evidence links |
| `GET /api/v1/tokens/by-mint/{mint}/analysis` | 10 | The same report by Solana mint address |
| `GET /api/v1/wallets/{address}` | 10 | Wallet insights, saved PnL, up to 100 holdings per page |
| `GET /api/v1/tokens/{id}/history` | 25 | Up to 1,000 saved analysis observations |
| `POST /api/v1/jobs/token_check` | 25 | One mint and metadata account read |
| `POST /api/v1/jobs/wallet_scan` | 100 | Up to 1,000 transactions and current holdings |
| Balance, pricing, order status, job status | 0 | Account request limits apply to authenticated reads |

Token IDs come from the token list. List cursors bind to a saved revision.
History cursors use the returned observation timestamp. Wallet scan cursors refer
to a completed job owned by the same account and address. Each next page has its
own price. PnL includes coverage, unknown buying costs, and quote-currency units.
Fresh wallet scans compute SOL and USDC PnL from finalized chain records.

## Start

1. Sign in at `/developers` and create a key. Store it in your password manager.
2. Send `Authorization: Bearer YOUR_API_KEY` on each data request.
3. Inspect `/api/v1/pricing` and `/api/v1/balance` for prices and usage.
4. Use `/api/v1/openapi.json` for the machine-readable endpoint reference.

Each account supports five active keys. Stored key hashes support lookup and
revocation. The creation response shows the new key once. Account export includes
key labels, balances, and purchase receipts. Account deletion removes keys and
usage data. Settlement identifiers remain as anonymous payment replay records.

## Token and wallet insights

The 10-credit reports include an `insights` object with version `api-insights-v1`.
Existing response fields stay available. Summaries use saved evidence. The report
adds a bounded history read and computes its summaries in memory.

A token report includes:

- Momentum score, model version, state, and the reasons behind the saved score.
- Score change since the previous observation, with its timestamp and elapsed
  seconds. A change is calculated when both observations use the same model.
  `model_changed` or `pending` keeps `score_delta` null.
- Each of the nine saved RATi checks as `met`, `flagged`, `pending`, or `skipped`.
  `review` means a check was flagged; `incomplete` means evidence is pending.
  Holder checks carry the basis `Largest token account sample`.
- Sampled swaps and buying/selling wallet counts, their time window, collection
  gaps, risk factors, and open checks. The saved report's scope stays visible.
- Mint/metadata facts and the latest saved control settings when available.

For an address from a launch or wallet, use the mint route directly:

```sh
curl "https://runners.rati.chat/api/v1/tokens/by-mint/SOLANA_MINT/analysis" \
  -H 'Authorization: Bearer YOUR_API_KEY'
```

This route resolves the saved Solana chain record. A mint awaiting its first
saved record returns 404 and returns any reserved credits.

A wallet report includes:

- History coverage (`complete`, `partial`, or `unknown`), observed transaction
  range, excluded transactions, and positions with known buying costs.
- The largest five token positions, their share of priced token holdings, and
  the number of holdings with prices. Native SOL balance has its own field.
- Realized and unrealized PnL grouped by SOL or USDC, with sale cost coverage.
  Each currency keeps its unit. Fees are reported separately in SOL.
- Wins, losses, flat sales, and win rate among sales with known costs in the
  latest 200 recorded trades. The sample size, time range, and sales with unknown
  costs accompany each result. A win rate uses `wins / known_cost_sales`.

Position cost coverage uses token positions other than SOL and USDC. Portfolio
shares use the priced token subtotal. Prices and cost data that are still pending
remain null. Partial history and unknown costs qualify the displayed PnL.

Wallet insights cover the whole saved snapshot, including holdings beyond the
first page. Pass `next_cursor` as `?cursor=...` to get the next 100 holdings for
10 credits. The cursor binds to that wallet and snapshot. A changed snapshot
returns 409 and returns the reserved credits. Start again with a new request key.
Fresh wallet scan results also include the same summaries for their covered data.

Every freshness object includes `as_of`, `evaluated_at`, `fresh_until`, and
`age_seconds`. Token observations, chain samples, and wallets use a 15-minute
freshness window; saved control lookups use five minutes. Missing or invalid
timestamps produce `unknown`. Each source has its own timestamp. Cached retries
preserve the original report; compare `fresh_until` with the current time when
using a replayed response.

## Safe retries

Use `Idempotency-Key` with 8–128 letters, digits, dashes, or underscores.
It is required for job submission and top-up orders, and optional for saved reads.
The same key and request details reuse the original charge. Changed details return
409. Completed read responses remain in Redis for one hour. Their charge receipts
remain for seven days; an expired response returns 410 so the client can choose a
new paid read. Failed reads return their credits and require a new key to retry.
Job results remain for seven days. Polling a job uses zero credits.

429 includes `Retry-After` and leaves the balance unchanged. A 402 credit response
includes the request price and top-up URL. Successful reads include usage, balance,
`X-Credits-Charged`, and `X-Request-ID`. Each snapshot includes its observation or
collection time. Missing saved data returns 404 and returns the reserved credits.

## x402 top-ups

Create an order with `POST /api/v1/topups`, an API key, an Idempotency-Key, and
`{"dollars":5}`. POST to the returned `payment_url` with the same API key using an
x402 v2 client. A 402 quote carries `PAYMENT-REQUIRED`. The client signs the quote
and retries with `PAYMENT-SIGNATURE`. A confirmed payment returns
`PAYMENT-RESPONSE` and adds 5,000 credits exactly once.

The official x402 SDK handles the payment format and Coinbase SDK authentication.
The server fixes the amount, network, USDC asset, and receiving address in the
order. A global payment claim and unique settlement receipt prevent a payment
from funding several accounts. Credit addition and the stored settlement commit
in one transaction. A completed order remains readable and safe to retry.

If settlement times out, the order becomes `pending`; the original proof remains
available for recovery. Retry the same order and payment. If the facilitator has
settled the transfer but returns an ambiguous reply on retry, operations must
reconcile that order against its chain receipt before issuing its credits. Keep
that order visible to the buyer. Mainnet release includes a lost-reply recovery
drill with the chosen facilitator.

## Payment settings and custody

Fly secrets hold receiving addresses and the Coinbase service credentials.
Wallet signing keys stay with the wallet provider or hardware wallet. The runtime
needs the public receiving address and payment-service credentials.

Use separate receiving addresses for test and production:

| Secret or setting | Purpose |
| --- | --- |
| `X402_MODE` | `test` (default) or `production` |
| `X402_TEST_BASE_PAY_TO` / `X402_TEST_SOLANA_PAY_TO` | Test receiving wallets |
| `X402_BASE_PAY_TO` / `X402_SOLANA_PAY_TO` | Production receiving wallets |
| `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` | Production facilitator authentication |
| `X402_PAYMENTS_ENABLED` | `1` enables quotes after setup checks pass |
| `API_FRESH_JOBS_ENABLED` | `1` enables bounded fresh jobs |
| `API_HELIUS_DAILY_CREDITS` | API allowance within the shared Helius budget; default 2,000, maximum 5,000 |

Manage production secrets through the Fly app's secret store. Keep the source
record in the owner's password manager. Use the Fly secret import command with
standard input for scripted setup. Keep secret values out of shell history,
repository files, build logs, and pull requests. Review address changes against
the wallet provider's receive screen. Existing orders retain their quoted address.

Before enabling production, run `python -m runner_web.api_payments` inside the
configured app. It validates receiving wallets, fetches supported schemes, and
prints the environment and supported network names. Testnet uses Base Sepolia and
Solana devnet; production uses Base and Solana mainnet through Coinbase.

## Capacity and storage

Redis admits requests atomically across web machines. Its service-wide ceiling is
300 requests per minute and eight concurrent requests. Each account can start two
fresh jobs per minute and has one active job. At most two jobs run across workers.
The queue holds up to 20 jobs. Admission checks the provider allowance before
reserving customer credits. Fresh reads reserve provider credits inside the
existing daily cap and price reserve. Jobs have a 15-minute
recovery deadline. Expired work returns its customer credits.

Read bodies stay in the short-lived Redis result cache, capped at 32 MiB of
compressed response data. Request bodies are limited to 16 KiB. PostgreSQL stores
compact charge receipts and bounded job results (512 KiB maximum). Cleanup runs with the
API worker, in batches of 1,000 expired receipts. The API returns saved token
analysis and chain-derived PnL. Raw stock-price and sports-odds exports can follow
a review of their feed terms.

## Sources checked on October 17, 2026

- [x402 seller SDK](https://docs.x402.org/getting-started/quickstart-for-sellers)
- [Coinbase facilitator](https://docs.cdp.coinbase.com/x402/seller/facilitator)
- [Helius metering](https://www.helius.dev/docs/billing/credits)
- [The Odds API terms](https://the-odds-api.com/terms-and-conditions.html)
