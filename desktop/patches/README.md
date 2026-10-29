# Svelte build plugin patch

The patch applies to `@sveltejs/vite-plugin-svelte` 7.3.1. Its old `deepmerge`
dependency is affected by CVE-2026-93753 and has no published fixed release.
The scoped override in `package.json` supplies `deepmerge-ts` 8.0.2 through
that dependency name. The patch selects its API and keeps the plugin's array
replacement and undefined-value rules.

`pnpm install --frozen-lockfile` checks the patch hash. Docker copies this
directory before installation. `node --test test/build-options.test.mjs`
checks the merge rules, prototype safety and the plugin's production options.
The renderer build also covers the patched plugin.

Remove the patch, override and direct test dependency when an upstream plugin
release uses a maintained merge library. Refresh the lockfile, run the build
and tests, and require a passing dependency audit.
