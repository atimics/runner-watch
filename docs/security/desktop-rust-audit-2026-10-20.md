# Desktop dependency audit

Updated October 28, 2026 from main `ca1d8c0`. This carries the opener change
from PR #598 onto current main and adds the Tokio safety update.

## Changes

The app opens checked external URLs with `open::that_detached`. This is the
same URL-opening function the opener plugin used. The existing host checks,
command name and Windows ShellExecute support stay in place.

Removing the opener plugin also removes its unused D-Bus dependencies:

| Package | Advisory |
| --- | --- |
| zbus_names 4.3.4 | [RUSTSEC-2026-0371](https://rustsec.org/advisories/RUSTSEC-2026-0371.html) |
| zvariant 5.15.0 | [RUSTSEC-2026-0370](https://rustsec.org/advisories/RUSTSEC-2026-0370.html) |
| zvariant_derive 5.15.0 | [RUSTSEC-2026-0372](https://rustsec.org/advisories/RUSTSEC-2026-0372.html) |

Tokio moves from 1.53.1 to 1.53.3. RustSec lists 1.53.3 as a patched release
for [RUSTSEC-2026-0386](https://rustsec.org/advisories/RUSTSEC-2026-0386.html).
Cargo generated the updated lockfile from the package manifests.

## Findings that require source review

- **GLib 0.18.5:** the local source includes the exact upstream fix for
  [RUSTSEC-2024-0429](https://rustsec.org/advisories/RUSTSEC-2024-0429.html).
  The source verifier checks the published file hashes and the two pointer
  changes. The optimized iterator regression tests passed on macOS. Version
  scans still report the package under its original version.
- **proc-macro-error 1.0.4:**
  [RUSTSEC-2024-0370](https://rustsec.org/advisories/RUSTSEC-2024-0370.html)
  reports an unmaintained build dependency in the Linux GTK macros. Replacing
  it requires an upstream bindings update. Track that work with the backport
  removal steps in `desktop/src-tauri/patches/README.md`.

The source verifier and full dependency scan remain active. Their results
keep these two findings visible for review.

## Validation

- Source verification passed for 121 GLib files and 19 URLPattern files.
- All 11 source-verifier tests passed.
- All seven optimized native GLib and URLPattern tests passed on macOS.
- All 39 frontend tests passed. Svelte reported zero errors and warnings.
- The desktop renderer production build and Rust format check passed.
- Cargo Audit reported zero vulnerabilities and the two reviewed warnings
  listed above. The removed D-Bus packages and old Tokio finding were absent.

Run Desktop Builds on this branch for the Linux, macOS and Windows packages.
Keep the PR in draft until those native checks finish.
