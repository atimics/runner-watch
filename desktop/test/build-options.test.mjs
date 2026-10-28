import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { deepmergeCustom } from 'deepmerge-ts';
import { resolveConfig } from 'vite';

const require = createRequire(import.meta.url);
const pluginRoot = dirname(require.resolve('@sveltejs/vite-plugin-svelte'));
const { preResolveOptions, resolveOptions } = await import(
  pathToFileURL(join(pluginRoot, 'utils/options.js')).href
);
const merge = deepmergeCustom({ mergeArrays: false, filterValues: false });

test('config merges keep nested values and replace arrays', () => {
  const first = { compiler: { dev: true, options: ['first'], enabled: true } };
  const second = { compiler: { options: ['second'], enabled: undefined } };
  const result = merge(first, second);
  assert.deepEqual(result, {
    compiler: { dev: true, options: ['second'], enabled: undefined },
  });
  assert.deepEqual(first.compiler.options, ['first']);
});

for (const location of ['target', 'source', 'null target']) {
  test(`prototype keys stay data in the ${location}`, () => {
    const payload = JSON.parse('{"__proto__":{"poisoned":true}}');
    const target = location === 'target' ? payload : location === 'null target' ? Object.create(null) : {};
    const source = location === 'target' ? {} : payload;
    const result = merge(target, source);
    assert.equal(Object.getPrototypeOf(result), Object.prototype);
    assert.equal(result.poisoned, undefined);
    assert.equal({}.poisoned, undefined);
  });
}

test('the patched Svelte plugin resolves production options', async () => {
  const root = await mkdtemp(join(tmpdir(), 'rati-build-options-'));
  const script = () => ({ code: '' });
  try {
    const viteConfig = await resolveConfig(
      { root, configFile: false }, 'build', 'production', 'production',
    );
    const prepared = await preResolveOptions(
      {
        configFile: false,
        extensions: ['.custom'],
        preprocess: [{ script }],
        compilerOptions: { preserveWhitespace: true },
      },
      viteConfig,
      { command: 'build', mode: 'production' },
    );
    const resolved = resolveOptions(prepared, viteConfig);
    assert.deepEqual(resolved.extensions, ['.custom']);
    assert.equal(resolved.preprocess[0].script, script);
    assert.equal(resolved.compilerOptions.preserveWhitespace, true);
    assert.equal(resolved.compilerOptions.css, 'external');
    assert.equal(resolved.compilerOptions.dev, false);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
