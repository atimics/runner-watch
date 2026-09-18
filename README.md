# runner-watch

Runner-watch is a free-data market intelligence service. It watches Stocks,
Solana Memecoins and Sports for early movers, records the evidence behind each
call, and shows the result on shared List, Detail and Map screens.

The public product runs at <https://runners.rati.chat> and
<https://sports.rati.chat>. A desktop client (`desktop/`) ships the local
scanner and can attach to the service through a signed node identity.

## What runs where

| Process | Entry point | Role |
| --- | --- | --- |
| Web | `uvicorn runner_web.main:app` | FastAPI + Jinja screens, JSON APIs, WebAuthn login |
| Worker | `stonks-worker` | ~20 background collectors, scans, settlement and delivery tasks |
| Trainer | `stonks-trainer` | Ranker training and historical backfill |
| Desktop | `desktop/` (Tauri) | Local scanner, node API and swarm transport |
| Edge | `cloudflare-router/` | Public host routing in front of Fly |

`PROCESS_ROLE` selects the role (`web`, `worker`, `trainer`, or `all` for local
development). Split roles require `REDIS_URL` for shared rate limits, jobs and
cache.

## Repository layout

```
src/runner_web/     Online service: routes, data workers, product logic
src/runner_watch/   Market scanner, EDGAR, risk and scoring
src/runner_node/    Desktop node API and local scan runtime
src/runner_swarm/   Signed node identity, transport and reputation
rust/stonks-ranker/ Integer ranker compiled into the production image
ml/sec-qwen/        SEC fine-tuning sources
web/                Jinja templates and static assets
desktop/            Tauri desktop client
cloudflare-router/  Edge worker
tests/              Unit and Playwright browser suites
docs/               Product and design documents (see docs/README.md)
scripts/            Operational and release scripts
```

## Local development

Requirements: Python 3.11–3.13, [`uv`](https://docs.astral.sh/uv/), and Node for
the desktop client.

```sh
uv sync --extra dev            # install the service and test tools
uv run stonks-migrate          # create/migrate the SQLite database
uv run uvicorn runner_web.main:app --reload --port 8080
```

The full stack (Postgres, Redis, migrate, web, worker, trainer) runs with
Docker:

```sh
docker compose -f compose.local.yml up --build
```

`compose.local.yml` is the reference for the production process boundaries and
for local-only values. Production values live in `fly.toml`.

### Testing

```sh
uv run pytest                                     # unit suite (browser tests excluded)
uv run pytest -m browser -o addopts=              # Playwright browser suite
uv run ruff check src tests ml/sec-qwen/src       # lint
```

The two suites can run in one process: `tests/conftest.py` suspends
Playwright's private event loop around non-browser tests. Running the browser
suite alone remains the fastest path.

## Configuration

Configuration is environment-only. 163 variables are read across
`src/`; the values below are the ones that change behavior or are required in
production. Unset optional features simply stay off.

**Runtime and host**

- `APP_ORIGIN`, `RUNNERS_ORIGIN`, `SPORTS_ORIGIN`, `LEGACY_ORIGIN` — public hosts.
- `RP_ID`, `LEGACY_RP_ID`, `COOKIE_SECURE`, `COOKIE_DOMAIN` — WebAuthn and cookies.
- `PROCESS_ROLE`, `BACKGROUND_WORKERS_ENABLED`, `WORKER_EXPECTED_INSTANCES`.
- `TRUST_FLY_CLIENT_IP`, `EDGE_PROXY_SECRET`, `REQUIRE_EDGE_PROXY_SECRET`.

**Storage**

- `DATABASE_URL` or `DATABASE_PATH`, `REQUIRE_DATABASE_URL`, `REQUIRE_DATABASE_TLS`,
  `ALLOW_NEWER_DATABASE_SCHEMA`.
- `REDIS_URL`, `REQUIRE_REDIS_TLS`, `RATE_LIMIT_HASH_KEY`,
  `REQUIRE_RATE_LIMIT_HASH_KEY`.

`ALLOW_NEWER_DATABASE_SCHEMA=1` is deliberate in production: the release command
migrates before the new image serves traffic, and a rollback must tolerate a
schema written by the newer release. Keep it off elsewhere.

**Secrets** (never commit; set through `flyctl secrets` or the desktop vault)

- `OPENROUTER_API_KEY`, `HELIUS_API_KEY`, `MASSIVE_API_KEY`, `ODDS_API_KEY`,
  `FINTEL_API_KEY`, `COURTLISTENER_API_TOKEN`, `SAM_API_KEY`, `STRIPE_SECRET_KEY`,
  `STRIPE_WEBHOOK_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
  `OPERATIONS_TOKEN`, `SWARM_NODE_PRIVATE_KEY`.

Use `scripts/configure-production-security` to generate the edge secret,
operations token and invite codes before the first production deploy.

**Feature switches** default to off and are turned on per environment:
`DISCOVERY_SOURCES_ENABLED`, `SPORTS_INGESTION_ENABLED`, `ODDS_API_ENABLED`,
`SEC`/`HOUSE_DISCLOSURES_ENABLED`, `MEMECOINS_ENABLED`,
`FINTEL_SHORT_DATA_ENABLED`, `ROBINHOOD_CHAIN_ENABLED` (read-only Stock Token
metadata on the stock detail), `TELEGRAM_*_ALERTS`, and the `RANKER_*` training
knobs. Budget caps and intervals (`HELIUS_DAILY_CREDITS`,
`ODDS_API_MONTHLY_WORKING_LIMIT`, `BACKGROUND_SCAN_INTERVAL_SECONDS`, …) are
documented in the module that owns them.

## Deployment

Pushes to `main` go through `.github/workflows/fly.yml`:

1. Lint, type-free unit tests, browser tests, dependency audit and Docker build.
2. Save the current release image for rollback.
3. Deploy, deploy the edge worker, keep worker/trainer processes alive and
   heartbeating.
4. Smoke-test both origins and render every production screen.
5. Roll back automatically and fail the run if any step fails.

Rollback is also manual: `flyctl deploy --image <previous-image>
--skip-release-command`.

## Documentation

Start at [docs/README.md](docs/README.md). It marks which design documents are
current and which are historical records.