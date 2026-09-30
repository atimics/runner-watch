import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import { handleAudits, PAGE_CSP, VAULT_CSP } from "../src/audits.js";
import worker from "../src/index.js";

const UUID = "3f1c1c1e-1b7a-4c3e-9a55-0d4f6a1b2c3d";
const WALLET = "FANZg2wvKX7277gD2vfSxvFZTSamoUZUGuAQc6CP7JAT";
const ADDRESS = "A".repeat(43);
const sha = (text) => createHash("sha256").update(text).digest("hex");

class FakeBucket {
  constructor(objects) {
    this.objects = objects;
  }
  async get(key) {
    if (!(key in this.objects)) return null;
    const text = this.objects[key];
    return { text: async () => text, arrayBuffer: async () => new TextEncoder().encode(text).buffer, body: new Response(text).body };
  }
  async head(key) {
    return key in this.objects ? { key } : null;
  }
}

const status = (over = {}) =>
  JSON.stringify({
    uuid: UUID,
    checked: "2026-09-29",
    protocol: { version: "2.7.0" },
    record_sha256: "r".repeat(64),
    findings: { total: 14, open: 12, fixed: 1, acknowledged: 1 },
    gates: ["phase not done: manual_review"],
    reviewers: [{ wallet: WALLET, findings: ["001-C1"], counts: true }],
    disclosure: { policy: "public-after-fix", max_wait_days: 90 },
    ...over,
  });

function finalObjects({ tamper = false } = {}) {
  const files = { "report.md": "# Audit 001\n", "attestation.json": JSON.stringify({ title: "Vault", findings: { total: 2, fixed: 2, acknowledged: 0 } }) };
  const manifest = JSON.stringify({ audit: "001", signer: "Jon", signed: "2026-09-29", protocol: { version: "2.7.0" }, files: Object.fromEntries(Object.entries(files).map(([n, t]) => [n, sha(t)])) });
  const objects = { [`final/${ADDRESS}/manifest.json`]: manifest, [`final/${ADDRESS}/allowed_signers`]: "Jon ssh-ed25519 AAAA\n" };
  for (const [name, text] of Object.entries(files)) objects[`final/${ADDRESS}/${name}`] = text;
  if (tamper) objects[`final/${ADDRESS}/report.md`] = "# softened\n";
  return objects;
}

const get = (path, env, method = "GET") => handleAudits(new Request(`https://trust.rati.chat${path}`, { method }), env);
const draftEnv = (extra = {}) => ({ AUDITS: new FakeBucket({ [`draft/${UUID}/status.json`]: status(), [`draft/${UUID}/vault.html`]: "<script>const VAULT = {}</script>", ...extra }) });

test("a draft page shows progress and never code", async () => {
  const response = await get(`/draft/${UUID}`, draftEnv());
  const page = await response.text();
  assert.equal(response.status, 200);
  assert.match(page, /An audit in progress/);
  assert.match(page, /phase not done: manual_review/);
  assert.ok(page.includes(WALLET) && page.includes("Open the sealed report"));
  assert.equal(response.headers.get("content-security-policy"), PAGE_CSP);
  assert.ok(!PAGE_CSP.includes("script-src"), "pages run no scripts");
});

test("hostile text in a status file is escaped, not run", async () => {
  const env = draftEnv({ [`draft/${UUID}/status.json`]: status({ gates: ['<img src=x onerror="alert(1)">'], reviewers: [{ wallet: "<script>x</script>", findings: [], counts: false }] }) });
  const page = await (await get(`/draft/${UUID}`, env)).text();
  assert.ok(!page.includes("<img src=x") && !page.includes("<script>x</script>"));
  assert.ok(page.includes("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"));
});

test("only the vault gets the relaxed policy", async () => {
  const vault = await get(`/draft/${UUID}/vault`, draftEnv());
  assert.equal(vault.status, 200);
  assert.equal(vault.headers.get("content-security-policy"), VAULT_CSP);
  assert.equal(vault.headers.get("cache-control"), "no-store");
  assert.match(await vault.text(), /const VAULT/);
  const json = await get(`/draft/${UUID}/status.json`, draftEnv());
  assert.equal(json.headers.get("content-type"), "application/json; charset=utf-8");
  assert.equal((await json.json()).findings.total, 14);
  const page = await get(`/draft/${UUID}`, draftEnv());
  assert.notEqual(page.headers.get("content-security-policy"), VAULT_CSP);
});

test("a draft without a vault does not offer one", async () => {
  const env = { AUDITS: new FakeBucket({ [`draft/${UUID}/status.json`]: status() }) };
  assert.ok(!(await (await get(`/draft/${UUID}`, env)).text()).includes("Open the sealed report"));
  assert.equal((await get(`/draft/${UUID}/vault`, env)).status, 404);
});

test("unknown and malformed drafts are not found", async () => {
  const env = draftEnv();
  for (const path of ["/draft/00000000-0000-4000-8000-000000000000", "/draft/not-a-uuid", `/draft/${UUID}%0A`, `/draft/${UUID}/other`, `/draft/${UUID}/vault/x`, "/draft/..%2F..%2Fx", "/draft"]) {
    assert.equal((await get(path, env)).status, 404, path);
  }
});

test("a final bundle lists its files with recomputed hashes", async () => {
  const env = { AUDITS: new FakeBucket(finalObjects()) };
  const page = await (await get(`/${ADDRESS}`, env)).text();
  assert.match(page, /Match their hashes/);
  assert.ok(page.includes("Vault") && page.includes(`/${ADDRESS}/report.md`));
  const file = await get(`/${ADDRESS}/report.md`, env);
  assert.equal(await file.text(), "# Audit 001\n");
  assert.match(file.headers.get("content-type"), /^text\/markdown/);
});

test("a changed file is shown as not matching", async () => {
  const page = await (await get(`/${ADDRESS}`, { AUDITS: new FakeBucket(finalObjects({ tamper: true })) })).text();
  assert.match(page, /Do not match their hashes/);
  assert.match(page, /does not match/);
});

test("only listed bundle files are served", async () => {
  const objects = { ...finalObjects(), [`final/${ADDRESS}/secret.txt`]: "nope" };
  const env = { AUDITS: new FakeBucket(objects) };
  assert.equal((await get(`/${ADDRESS}/secret.txt`, env)).status, 404);
  assert.equal((await get(`/${"B".repeat(43)}`, env)).status, 404);
  assert.equal((await get(`/${ADDRESS}/report.md/extra`, env)).status, 404);
});

test("a manifest cannot make the page read an unlisted object", async () => {
  const objects = finalObjects();
  const manifest = JSON.parse(objects[`final/${ADDRESS}/manifest.json`]);
  manifest.files["../draft/x/status.json"] = "0".repeat(64);
  objects[`final/${ADDRESS}/manifest.json`] = JSON.stringify(manifest);
  const page = await (await get(`/${ADDRESS}`, { AUDITS: new FakeBucket(objects) })).text();
  assert.match(page, /Do not match their hashes/);
});

test("anything that is not an audit path is left for the app", async () => {
  const env = draftEnv();
  for (const path of ["/", "/rules.schema.json", "/health", "/trust", "/stock/AAPL", "/api/trust/access-requests"]) {
    assert.equal(await get(path, env), null, path);
  }
  const post = await handleAudits(new Request(`https://trust.rati.chat/draft/${UUID}`, { method: "POST" }), env);
  assert.equal(post, null);
});

test("HEAD answers with headers and no body", async () => {
  const response = await get(`/draft/${UUID}`, draftEnv(), "HEAD");
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "");
});

test("the router answers audit paths from R2 on the trust host and proxies the rest", async () => {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (request) => {
    calls.push(new URL(request.url).hostname);
    return new Response("from the app");
  };
  try {
    const env = { ...draftEnv(), EDGE_PROXY_SECRET: "s" };
    const audit = await worker.fetch(new Request(`https://trust.rati.chat/draft/${UUID}`), env);
    assert.equal(audit.status, 200);
    assert.equal(calls.length, 0, "an audit path never reaches the app");
    const rest = await worker.fetch(new Request("https://trust.rati.chat/rules.schema.json"), env);
    assert.equal(await rest.text(), "from the app");
    assert.deepEqual(calls, ["runner-watch-ratimics.fly.dev"]);
    const other = await worker.fetch(new Request(`https://runners.rati.chat/draft/${UUID}`), env);
    assert.equal(await other.text(), "from the app", "other hosts never read the audit bucket");
    const unbound = await worker.fetch(new Request(`https://trust.rati.chat/draft/${UUID}`), { EDGE_PROXY_SECRET: "s" });
    assert.equal(await unbound.text(), "from the app", "without the binding it proxies as before");
  } finally {
    globalThis.fetch = original;
  }
});
