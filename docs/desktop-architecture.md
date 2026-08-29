# RATi desktop and scanner architecture

RATi has one open-source product engine and one open-source client. RATi AI Cloud runs the same
scanner and client in managed mode; it is not a second scoring implementation.

```text
RATi App -> RATi Node API -> RATi Scanner -> providers and local storage
                                  |
                                  +-> optional OpenRouter connection
                                  +-> optional RATi AI Cloud services
```

## Packages

- `runner_watch` remains the provider-neutral scanner and scoring core.
- `runner_node` owns the versioned Node API, provider discovery, scanner execution, and user
  connections. `rati-scanner` starts it as a loopback service by default.
- `desktop` is the Svelte and Electron client. It contains no scanning or provider logic.
- `runner_web` remains the hosted product while its rendered screens move to the shared Node API.
  It exposes the same Node router during the migration.

## Connection modes

The app connects to exactly one node URL at a time.

| Mode | Node | Storage | Provider credentials |
| --- | --- | --- | --- |
| Desktop | Bundled local scanner | Local storage; SQLite is the persistence target | OS credential vault or environment |
| Self-hosted | Standalone scanner | SQLite or Postgres | OS vault, environment, or secret manager |
| RATi AI Cloud | Managed scanner roles | Postgres and Redis | RATi-managed secrets |

When no node is connected, the app is in Library mode. It may show cached or imported receipts,
but it cannot label data as live or create new scans, research, Calls, picks, or alerts.

## Node API v1

The first vertical slice keeps scan receipts in a bounded in-memory store and provides:

```text
GET    /api/v1/node
GET    /api/v1/providers
POST   /api/v1/scans
GET    /api/v1/scans/{scan_id}
GET    /api/v1/connections/openrouter
POST   /api/v1/connections/openrouter/start
GET    /api/v1/connections/openrouter/flows/{flow_id}
GET    /api/v1/connections/openrouter/callback/{flow_id}
PUT    /api/v1/connections/openrouter
DELETE /api/v1/connections/openrouter
```

Cloud nodes do not accept user-triggered scans through the unauthenticated local endpoint. Their
managed workers continue to populate the existing shared Pulse and Radar data.

## OpenRouter

The recommended local connection uses OpenRouter OAuth with an S256 PKCE challenge. The scanner
creates the verifier, receives the loopback callback, exchanges the one-use code, and writes the
resulting key to the credential vault. The Svelte renderer sees only connection state and a short
key fingerprint. Environment variables remain available for headless deployments, and direct key
entry remains an advanced fallback.

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
- The bundled scanner binds to `127.0.0.1` on a random port.

## Native builds

The desktop workflow builds the Python scanner as a per-platform executable, stages it as an
Electron resource, runs Svelte checks and tests, then creates:

- a Debian package on Linux;
- a DMG and ZIP on macOS;
- a Squirrel installer on Windows.

Artifacts are retained for 14 days. Release signing and notarization should be added before public
distribution; pull request artifacts are intentionally unsigned.

## Next slices

1. Persist scan runs and expose Pulse, Radar, ticker, and chart read models through `/api/v1`.
2. Move local provider settings behind write-only credential endpoints.
3. Add authenticated cloud node sessions and optional local-to-cloud artifact sync.
4. Move Calls, picks, research receipts, and exports to the shared API.
5. Add the Rust ranker binary to the packaged scanner resources.
6. Replace the hosted Jinja screens with the same Svelte build after feature parity.
