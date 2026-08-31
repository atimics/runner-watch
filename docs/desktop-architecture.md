# RATi Swarm desktop and source architecture

RATi Swarm is a local workspace that combines scanner and market-data sources. The built-in
scanner is one source. RATi is another source that can be enabled for free. Users can add any
compatible remote scanner, and each remote scanner remains an independent source.

```text
RATi Swarm desktop
       |
       +-> local source hub and credential vault
               |
               +-> built-in scanner
               +-> RATi source (optional, preconfigured)
               +-> remote scanner A (optional URL and token)
               +-> remote scanner B (optional URL and token)
               +-> Yahoo, SEC, Nasdaq, and other no-key sources
               +-> Massive, Fintel, and other optional key sources
```

Pulse and Radar use the newest receipt from every enabled scanner source. RATi items have a purple
source marker, built-in scanner items have a green marker, and added scanners receive stable colors.
Receipts keep the producing source attached so results are not presented as if they came from one
global feed.

## Packages

- `runner_watch` is the provider-neutral scanner and scoring core.
- `runner_node` owns the local source hub, source registry, scanner execution, credential storage,
  and versioned API. `rati-scanner` starts it as a loopback service by default.
- `desktop` is the Svelte and Tauri 2 client. A small Rust host owns the native window and bundled
  scanner process; it contains no scoring or provider logic.
- `runner_web` remains the separately deployed website.

## Source types

| Source | Default | Setup | Result color |
| --- | --- | --- | --- |
| Built-in scanner | Enabled | None | Green |
| RATi | Available | One enable button | Purple |
| Compatible remote scanner | User-added | HTTPS origin and optional token | Stable source color |
| Included market-data source | Enabled when approved | None | Source color |
| Optional provider | Available | API key | Source color |

The bundled scanner persists its own receipts in SQLite. RATi and remote receipts are read through
the local source hub and stay labelled with their source. Remote tokens and optional provider keys
are write-only from the renderer's point of view and are stored in the operating-system credential
vault.

## Source API v1

```text
GET    /api/v1/node
GET    /api/v1/providers
GET    /api/v1/source-scans
GET    /api/v1/tickers/{ticker}
GET    /api/v1/scans
POST   /api/v1/scans
GET    /api/v1/scans/{scan_id}
PUT    /api/v1/sources/rati-cloud
POST   /api/v1/connections/scanners
DELETE /api/v1/connections/scanners/{scanner_id}
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

Source reads, scan receipts, research, OAuth flow control, and credential changes require the local
hub bearer token. The bundled desktop process creates a new token on every launch. A self-hosted
hub refuses to start until `RATI_NODE_TOKEN` contains at least 24 characters.

Remote scanner origins must use HTTPS. Loopback scanners may use HTTP. The source client refuses
redirects so it cannot forward a scanner token to another host, bounds response sizes, and validates
receipt shapes before they enter the workspace.

## Free and optional data

The built-in scanner uses Yahoo market bars without an API key and falls back between configured
providers through the shared provider registry. SEC uses the app's built-in contact identity.
Other approved no-key sources appear ready automatically.

Massive, Fintel, and The Odds API are optional. Their cards accept a key directly and never return
the stored value to the renderer. OpenRouter uses the same write-only rule and also supports its
OAuth PKCE flow.

## Desktop security

- The renderer runs in the operating-system webview and has no Node.js access.
- A narrow Tauri bridge exposes runtime details and approved external links.
- The bundled scanner binds an exclusive random loopback port and announces it to Tauri.
- Local API access requires a fresh per-launch bearer token.
- Remote scanner tokens and provider keys stay in the operating-system credential vault.
- Navigation stays inside the packaged app; Rust checks external links before opening them.

## Native builds

The desktop workflow freezes the Python source hub as a per-platform sidecar, stages it with the
Tauri application, checks its API, runs Svelte and Rust checks and tests, and creates Linux, macOS,
and Windows packages. The scanner and scoring kernel remain Python; Rust owns only the native shell
and process lifecycle.
