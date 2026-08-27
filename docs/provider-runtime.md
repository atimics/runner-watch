# Provider and live-data runtime

Stonks keeps outside providers separate from product code. Pulse, Radar, Alpha, and the scanner
request a canonical data kind. A provider adapter is responsible for turning its response into the
shared contract.

```text
Provider adapter -> canonical FetchBatch -> audited ingestion -> topic snapshot -> product view
```

## Canonical contracts

`runner_watch.provider_contracts` defines bars, quotes, market events, issuer facts, macro
observations, requests, and provenance. Provider timestamps must include a timezone. The
provenance object records the provider, feed, locator, as-of time, collection time, delay state,
warnings, quality notes, and every provider attempted.

`ProviderRegistry` owns explicit routes. It tries providers in the written order. It can move to a
fallback after an exception, error response, or empty response. A partial response is returned as
partial and is not silently combined with a second provider. This protects timestamp and price
semantics.

The routes are:

```text
daily bars -> massive -> yahoo (when MASSIVE_API_KEY is set; otherwise yahoo only)
intraday bars -> yahoo
```

Massive serves end-of-day daily bars from its grouped daily endpoint. Its adapter keeps a local
SQLite cache of whole sessions and bounds scan-time backfill with `MASSIVE_MAX_SCAN_CALLS`; a cold
cache fails fast and falls back to Yahoo instead of stalling a scan. Run
`stonks-massive-backfill` nightly to keep the cache warm.

The scanner still receives pandas frames through `RoutedMarketData`, so scoring behavior does not
change. The frame compatibility layer now sits after the canonical boundary.

To add a provider:

1. Implement `ProviderAdapter` and declare its data capabilities.
2. Convert its payload to canonical records.
3. Record its raw fetch through the shared ingestion pipe.
4. Add its source and feed policy to `source_catalog.py`.
5. Register it and place it in an explicit route.
6. Test empty, partial, stale, rate-limit, and bad-timestamp responses in shadow mode.

## Topic freshness

`runner_web.topics.TopicHub` gives the web app shared topics such as
`market:bars:PEN:5m`. A topic policy has a cache TTL, minimum retry interval, maximum stale age, and
a last-known-good rule.

The hub:

- combines concurrent refreshes
- batches missing chart topics into one provider request
- returns cached data within its TTL
- keeps the last good value after a provider error
- marks values as fresh, stale, expired, or error
- saves snapshots in SQLite so restarts can reuse them
- evicts the least recently used in-memory topics after a fixed bound
- supports wildcard subscriptions for a later SSE or WebSocket layer

Chart APIs keep their old `charts` or `points` fields and now also return `freshness`. Freshness
contains source, as-of time, collection time, age, delay state, warnings, and the last refresh
error. Existing clients can ignore the new field.

## Database migrations

`init_db()` now applies ordered migrations recorded in `schema_migrations`. Migration 1 adopts and
upgrades the existing beta schema. Migration 2 adds durable topic snapshots. Source policies are
still seeded on every startup because their enabled state can depend on environment variables.

New schema changes must be added as the next numbered `Migration`; do not add more startup-only
column checks outside the migration list.
