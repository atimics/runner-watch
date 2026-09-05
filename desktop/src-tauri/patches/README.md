# Desktop dependency backports

The desktop uses the released Tauri 2.11.5 dependency family. These source backports keep that public API while applying two official upstream fixes. The sibling JSON files record the published archive checksums and every original file hash. Both crates retain their original version, MIT license, and copyright.

GLib 0.18.5 applies the two pointer changes from [gtk-rs commit b5a4071e439bef2b5eea76c3aa25e5ae84839e34](https://github.com/gtk-rs/gtk-rs-core/commit/b5a4071e439bef2b5eea76c3aa25e5ae84839e34). They repair the optimized string iterator crash described by [RUSTSEC-2024-0429](https://rustsec.org/advisories/RUSTSEC-2024-0429.html). The same release-mode regression crashes with the original crate and passes with this patch. Tauri's released Linux stack uses GTK 0.18 and GLib 0.18; moving to the published GLib fix requires a matching framework and bindings release.

URLPattern 0.3.0 applies the ICU tokenizer change from [Deno commit b047afee9b901e19928f87470ba722bbac05a27f](https://github.com/denoland/rust-urlpattern/commit/b047afee9b901e19928f87470ba722bbac05a27f). It removes five UNIC maintenance advisories and updates Unicode identifier handling. The normalized Cargo manifest carries the same dependency change. The public API remains 0.3. Tauri accepted URLPattern 0.6 in [PR 15660](https://github.com/tauri-apps/tauri/pull/15660), after the current framework release.

Run these checks from the repository root. The Rust tests need the system GLib development library; the Linux desktop workflow installs it.

```sh
python3 scripts/verify_desktop_backports.py
python3 -m unittest discover -s tests -p test_desktop_backports.py
cargo test --locked --release --manifest-path desktop/backport-regression/Cargo.toml
```

The source verifier allows only the recorded upstream changes. The optimized tests cover GLib iteration and URLPattern Unicode names, URL matching, and host boundaries. The existing full Rust audit remains active. Its version-based result still lists GLib 0.18.5 even though this source contains the fix, and it reports the remaining GTK/proc-macro maintenance advisories.

Remove each backport when a compatible released Tauri stack resolves that dependency to a maintained release with the fix. Then remove its Cargo patch, vendor directory, JSON provenance, verifier entry, and dedicated regression coverage; refresh the locks and run all desktop platform builds and the full dependency scan. GTK3's current development branch uses prerelease bindings, so verify published framework constraints at that time.
