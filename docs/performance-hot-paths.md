# Bounded reads and reusable outcome indexes

Baseline: `7e44356` (scoring-contract refactor). This change preserves score
formulas, eligibility, freshness/expiry rules, bar-boundary conventions, outcome
ambiguity and retry semantics. It does not cache market responses for longer.

## Evidence and targets

A read-only production sample on 2026-09-22 at 00:14 UTC found approximately
5.31 million archived bars, 68,000 scan outcomes, 71,000 snapshots and 15,000 SEC
filings. Sampled cached Pulse requests took roughly 0.19–0.40 seconds including
network transport. Those three samples are not a reliable production p95 or a
before/after endpoint benchmark. They motivated optimizing work beneath the
response cache instead of merely extending cache lifetimes.

The reproducible synthetic measurements are in `benchmarks/hot-paths.json`.
They include every timing sample, median ratios and exact-output parity checks.
Run from a checkout with history:

```sh
uv sync --frozen --extra dev
uv run python scripts/benchmark-hot-paths.py --baseline-ref 7e44356 --repeats 7
```

The script forces a temporary local SQLite database; it does not use production
credentials or a caller's configured database. The Pulse comparison loads only
the old input-loader function from the baseline Git revision. It filters the
baseline's unused off-board maps before checking equality with the new loader.
SQLite figures characterize the fixture, not PostgreSQL query latency. The SQL
translation microbenchmark excludes execution and network time.

## Changes

### Outcome history indexes

One batch-local `IndexedBars` per ticker replaces repeated full-history walks
with binary searches. The date-to-last-observation index is constructed lazily,
once per history, when a daily horizon is requested. This retains last-available
observation semantics; it does not invent official exchange closes from sparse
bars. The histories are not shared between collection cycles, so new archived
observations are visible on the next fetch.

Legacy sequence callers retain the existing implementations. Differential tests
cover gaps, duplicate timestamps, endpoint/tolerance inclusivity, same-bar
ambiguity and both US daylight-saving transitions. The benchmark includes index
construction for 200 observations across 10 ticker histories of 2,500 bars each.
Database row wrappers are iterated rather than all duplicated into a temporary
Python list. This is not a server-side streaming cursor: the PostgreSQL driver
may still buffer its result.

### Bounded outcome queue reads

The collector uses keyset pages to fill its existing oldest-due-first budget.
Completed rows are excluded from payload fetches; `COUNT(*)` preserves the old
`rows` telemetry. New `candidate_rows_examined` reports payloads materialized.
Maturity checks remain timezone-aware Python comparisons rather than unsafe
lexical comparisons of legacy TEXT timestamps. Equal-time observations now use
snapshot ID as a deterministic tie-breaker. Retry timestamps remain respected.

The first full page may satisfy the budget; the benchmark materializes 200 of
50,000 records. When many candidates are immature, selection continues across
pages rather than starving a later due row. That worst case still examines those
candidates. The count query and database filtering are not constant-time.

The legacy timestamp-repair UPDATE now writes only rows that differ from their
source snapshot. Actual repairs still run; already-correct rows no longer incur
no-op update/WAL work. No production data backfill or schema migration is added.

### Pulse context

Full-board reads restrict filings, market events and descriptive community
counts to the latest scan's ticker universe. Explicit ticker detail retains
context even when that ticker is outside the latest scan, or no scan exists.
Source-policy filtering and all knowledge-time constraints are unchanged.
Filing count and the highest-ranked filing now come from one windowed query,
removing a duplicate filing scan/round trip. Public attention and forecast
formulas are unchanged.

### SQL template translation

A 512-entry LRU caches the pure SQLite-to-PostgreSQL template translation. Bound
parameter values and query results are never cached. This removes repeated
regex/placeholder parsing in hot database paths without altering execution.
Tests verify cache bounds, quote/named-parameter behavior, concurrent calls and
independent parameter execution.

## Verification and limitations

`tests/test_performance_hot_paths.py` asserts parity and deterministic work
budgets, not flaky wall-clock thresholds. Standard unit, browser, Rust, container,
security and coverage gates remain in place. The benchmark receipt is generated
on the verification runner and committed with this change.

No whole-application speedup is claimed from these microbenchmarks. Production
benefit depends on candidate counts, history reuse, database plans and cache hit
rates. Review ordinary production telemetry after deployment; do not extrapolate
the SQL parsing ratio into an end-to-end response-time claim.
