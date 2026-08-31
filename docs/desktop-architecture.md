# RATi Swarm desktop and scanner architecture

RATi has one open-source product engine and one open-source client. RATi AI Cloud runs the same
scanner and client in managed mode; it is not a second scoring implementation.

```text
RATi Swarm -> public Pulse / Radar / Flash APIs -> RATi Cloud at runners.rati.chat
       |
       +-> selected Scanner API -> local or cloud scanner -> providers and storage
                                                    |
                                                    +-> optional OpenRouter
```

## Packages

- `runner_watch` remains the provider-neutral scanner and scoring core.
- `runner_node` owns the versioned Node API, provider discovery, scanner execution, and user
  connections. `rati-scanner` starts it as a loopback service by default.
- `desktop` is the RATi Swarm Svelte and Electron client. Pulse, Radar, and Flash are its main
  navigation. It contains no scanning or provider logic.
- `runner_web` remains the hosted product while its rendered screens move to the shared Node API.
  It exposes the same Node router during the migration.

## Connection modes

The Local/Cloud selector controls both the scanner and every visible data screen. Local mode never
requests, reads from cache, or renders the hosted product feed. Its Pulse and Radar screens use the
latest local scan receipt, and Flash shows local scan history. Cloud mode alone requests Pulse,
Radar, and Flash from `runners.rati.chat`. Scanner work connects to exactly one node URL at a time.

| Mode | Node | Storage | Provider credentials |
| --- | --- | --- | --- |
| Desktop | Bundled local scanner | SQLite receipts plus a small renderer cache | OS credential vault or environment |
| Self-hosted | Standalone scanner | SQLite receipts | OS vault, environment, or secret manager |
| RATi AI Cloud | Managed scanner roles | Postgres and Redis | RATi-managed secrets |

When no local scanner is connected, saved local receipts still populate Local Pulse, Radar, and
Flash, but the app cannot create new scans, ticker pulls, or AI research. When Cloud is selected and
the hosted service is unreachable, its three product screens clearly show their latest cloud cache
as offline. The two caches are never mixed in the interface.

## Node API v1

The scanner keeps a bounded receipt history in SQLite when `DATABASE_PATH` is set. It provides:

```text
GET    /api/v1/node
GET    /api/v1/providers
GET    /api/v1/tickers/{ticker}
GET    /api/v1/scans
POST   /api/v1/scans
GET    /api/v1/scans/{scan_id}
POST   /api/v1/research
GET    /api/v1/connections/openrouter
POST   /api/v1/connections/openrouter/start
GET    /api/v1/connections/openrouter/flows/{flow_id}
GET    /api/v1/connections/openrouter/callback/{flow_id}
PUT    /api/v1/connections/openrouter
DELETE /api/v1/connections/openrouter
PUT    /api/v1/connections/{provider}
DELETE /api/v1/connections/{provider}
```

Scan receipts, research, OAuth flow control, and credential changes require a bearer token. The
bundled desktop process creates a new token on every launch. A self-hosted scanner refuses to start
until `RATI_NODE_TOKEN` contains at least 24 characters. Public node and provider capability reads
remain available so the hosted client can explain what the selected node offers.

Local scan requests always use live providers. Yahoo market data and other enabled no-key sources
are connected automatically. Local ticker pages pull current daily and five-minute bars through
the same scanner and show their provider provenance. There is no sample-data request, synthetic
provider, or invented volume in production.

Cloud nodes do not accept user-triggered scans through the local endpoint. Their
managed workers continue to populate the existing shared Pulse and Radar data.

## OpenRouter

The recommended local connection uses OpenRouter OAuth with an S256 PKCE challenge. The scanner
creates the verifier, receives the loopback callback, exchanges the one-use code, and writes the
resulting key to the credential vault. The Svelte renderer sees only connection state and method.
Environment variables remain available for headless deployments, and users may paste their own key
as a direct fallback. Once connected, the standalone `/api/v1/research` route uses that same
node-owned key; the renderer never receives a stored key back from the API.

Massive, Fintel, and The Odds API keys use the same write-only credential contract. Provider
factories accept injected vault credentials as well as operator-managed environment variables. Free
sources require no setup and appear automatically from the shared source catalog.

Disconnecting removes the local credential. It does not revoke an environment-managed credential
or delete the key from the user's OpenRouter account; the app links to OpenRouter's key settings
for account-side management.

## Desktop security

- The renderer is sandboxed and has no Node.js access.
- Context isolation is enabled.
- The preload bridge exposes only runtime details and approved external links.
- Packaged assets use the secure `rati-app` protocol instead of `file://`.
- Navigation and permission requests are denied by default.
- Remote node URLs require HTTPS; loopback development nodes may use HTTP.
- The bundled scanner binds its own exclusive `127.0.0.1` random port and announces it to Electron.
- Local API writes require a fresh per-launch bearer token.
- Self-hosted API writes require an operator-configured bearer token.

## Native builds

The desktop workflow builds the Python scanner as a per-platform executable, stages it as an
Electron resource, launches that frozen executable and checks its API, runs Svelte checks and tests,
then creates:

- a Debian package on Linux;
- a ZIP on macOS;
- a Squirrel installer on Windows.

Artifacts are retained for 14 days. Release signing and notarization should be added before public
distribution; pull request artifacts are intentionally unsigned.

## Hosted app

The production container builds the same Svelte renderer used by Electron and serves it at
`/desktop/`. Cloud Scanner points to `https://runners.rati.chat`; Local points to the bundled or
self-hosted node. Existing Jinja screens and their public read APIs stay available during migration.

## Next slices

1. Add authenticated cloud scanner sessions and optional local-to-cloud artifact sync.
2. Move ticker detail, Calls, picks, research receipts, and exports into the shared client.
3. Add the Rust ranker binary to the packaged scanner resources.
4. Add signing, notarization, and automatic updates for public desktop releases.
