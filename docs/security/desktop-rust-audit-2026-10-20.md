# Desktop Rust audit review — 2026-10-20

Reviewed main `f1a556b` and Dependabot PR #586 head
`bbffca33224573614c6d91e5ad99613208f75b82`. Supported desktop release
platforms are Linux, macOS and Windows (`.github/workflows/desktop.yml`).

## Findings and exposure

| Advisory / locked package | Dependency path and actual use | Disposition |
| --- | --- | --- |
| [RUSTSEC-2024-0429](https://rustsec.org/advisories/RUSTSEC-2024-0429.html), glib 0.18.5 | App → Tauri / tauri-runtime-wry → GTK 0.18 / WebKitGTK → GLib, Linux runtime. Regression workspace member also depends directly on GLib. | Real upstream iterator unsoundness, fixed in published 0.20.0. App already uses a local 0.18.5 backport of the exact upstream pointer fix, verified against published-source hashes. Keep that compatible fix. Version scanning still reports it. |
| [RUSTSEC-2024-0370](https://rustsec.org/advisories/RUSTSEC-2024-0370.html), proc-macro-error 1.0.4 | Linux GTK stack → glib-macros 0.18.5 and gtk3-macros 0.18.2 → proc-macro-error. Executes during compilation, not as an application runtime parser. | Unmaintained notice with no patched release or identified exploit. Build dependency maintenance/supply-chain risk remains. Requires upstream macro/bindings migration; adding an unrelated newer direct dependency will not replace it. |
| [RUSTSEC-2026-0371](https://rustsec.org/advisories/RUSTSEC-2026-0371.html), zbus_names 4.3.4 | App → tauri-plugin-opener 2.6.0 → zbus 5.19.0 → zbus_names (also through zbus_macros). Plugin selects zbus on Linux/BSD. | Unmaintained standalone crate; replacement is zbus 6 names module. Removed with unused opener plugin dependency chain. |
| [RUSTSEC-2026-0370](https://rustsec.org/advisories/RUSTSEC-2026-0370.html), zvariant 5.15.0 | Same opener → zbus chain; wire-format library. | Unmaintained standalone crate; replacement is zbus 6 wire module. Removed from app lockfile. |
| [RUSTSEC-2026-0372](https://rustsec.org/advisories/RUSTSEC-2026-0372.html), zvariant_derive 5.15.0 | Same chain → zvariant / zbus_macros → zvariant_derive, compilation-time macro. | Unmaintained standalone crate; replacement macros provided by zbus 6. Removed from app lockfile. |

The four maintenance notices do not describe concrete memory-safety bugs or
known attacks. That does not establish that the packages are free of defects.
The application does not directly call VariantStrIter; indirect GTK use cannot
be ruled out, so the existing source fix remains necessary. macOS and Windows
use their native webview stacks rather than GTK. A workspace-wide lock scan
also includes the regression-only GLib dependency, regardless of app target.

## Scoped correction

Runner Watch only called the opener plugin to open a URL after
`safe_external_url` validation. It never used file reveal or plugin JavaScript
commands (the capability grants only core permissions). Replace that plugin
with `open::that_detached`, the same implementation used by the plugin's
default desktop URL-opening path, keeping `shellexecute-on-windows` enabled.
Keep the URL validation and command name unchanged. No shell-string command
construction is introduced. This removes unused D-Bus/file-reveal support,
not the scanner sidecar shell plugin. OS association/browser-launch smoke
checks on all three platforms remain necessary.

Cargo regenerated the lockfile, pruning the opener-only closure; no package
versions were added or upgraded. The existing `open` 5.4.2 was already locked
through tauri-plugin-shell. Source backports and vulnerability detection are
unchanged; no suppressions were added.

## PR #586 and compatible alternatives

The proposed lockfile updates serde_json, tauri, tauri-build, opener, shell,
tokio and uuid. Its diff does not change any of the five reported package
versions. It therefore remediates none of these findings. Released opener
2.7.1 still requires zbus `5.9` (Cargo caret range excludes 6), as confirmed
from its published crate manifest; forcing zbus 6 into that transitive edge
is not a compatible lockfile update. Removing the unused plugin avoids a
custom fork. PR #586 must be rebased after this change so it does not
reintroduce opener; its unrelated updates can be reviewed separately.

GLib 0.20 cannot replace 0.18 within the GTK 0.18 API family. Preserve the
verified backport until a compatible framework/bindings release removes that
constraint. Follow the removal checklist in
`desktop/src-tauri/patches/README.md`. Recheck upstream GTK macros for a
maintained proc-macro-error replacement at that time.

## Validation and limits

- `python3 scripts/verify_desktop_backports.py`: passed (121 GLib and 19 URLPattern files).
- `python3 -m unittest discover -s tests -p test_desktop_backports.py`: 11 passed.
- `cargo fmt --manifest-path desktop/src-tauri/Cargo.toml --check`: passed.
- `pnpm exec vitest run`: 39 tests passed across seven files.
- `pnpm exec svelte-check --tsgo --tsconfig ./tsconfig.json`: zero errors/warnings.
- `pnpm build`: passed.
- Cargo inverse tree confirmed both Linux proc-macro paths; Windows app inverse tree has no proc-macro-error path.
- OSV Scanner 2.7.0 on the desktop Rust lock: only GLib (RustSec plus GHSA alias) and proc-macro-error remain. All three D-Bus advisories are absent. Audit intentionally remains non-passing.
- Full workflow-equivalent scan (`--lockfile=./rust/stonks-ranker/Cargo.lock --recursive ./desktop`) additionally reports three Vite advisories, outside these five Rust findings: GHSA-587h-7wmj-h5pm, GHSA-698v-4r3x-3vcv, GHSA-r825-w8mm-5pxw. These require separate frontend triage.
- Native `cargo check`, desktop tests and optimized backport tests were attempted, but cannot complete here: pkg-config and GLib/GTK/WebKit development libraries are missing. Package installation failed due to host identity restrictions. This is not a passing native test result. No macOS/Windows native build or browser-launch smoke result is claimed.

Before merging, run the existing Desktop Builds workflow on this branch for
Linux/macOS/Windows native checks, optimized backport tests and packaged
smoke tests. PR runs currently skip its native build matrix, so green PR
metadata/frontend checks alone are insufficient. Keep this change in draft
until native validation is available.
