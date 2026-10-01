import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { handleAudits } from "../src/audits.js";
import { appEnv, githubStub, sha256, wallet } from "../testing/helpers.js";

const UUID = "3f1c1c1e-1b7a-4c3e-9a55-0d4f6a1b2c3d";
const ENC = "07203f7d08cb9ff98b92f6e4d50dba339905946ddeee2a681a7b8efb50ffee7a";
const statement = (audit, enc) => `ratiaudit enrol v1\naudit: ${audit}\nencryption key: ${enc}`;
let stub;
afterEach(() => stub?.restore());

async function setup(known, { review } = {}) {
  const status = JSON.stringify({ uuid: UUID, review: { audit: "001", message: "m", reviewers: review ?? [] }, known: await Promise.all(known.map((w) => sha256(w.address))) });
  const { env } = await appEnv({ [`draft/${UUID}/status.json`]: status });
  return env;
}

const post = (body, env, headers = {}, path = `/draft/${UUID}/access-request`) =>
  handleAudits(new Request(`https://trust.rati.chat${path}`, { method: "POST", headers: { "Content-Type": "application/json", ...headers }, body: typeof body === "string" ? body : JSON.stringify(body) }), env);
const inbox = (env) => Object.keys(env.AUDITS.objects).filter((k) => k.startsWith("inbox/"));

async function request(w, over = {}) {
  return { audit: "001", wallet: w.address, enc_pub: ENC, signature: await w.sign(statement("001", ENC)), ...over };
}

test("a known wallet's signed request is kept, and starts the sync job", async () => {
  stub = githubStub();
  const w = await wallet();
  const env = await setup([w]);
  const response = await post(await request(w), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  assert.equal(JSON.parse(env.AUDITS.objects[`inbox/${UUID}/enrol-${w.address}.json`]).enc_pub, ENC);
  assert.equal(stub.dispatches(), 1);
});

test("a wallet that is not on the known list is turned away and nothing is stored or started", async () => {
  stub = githubStub();
  const known = await wallet();
  const stranger = await wallet();
  const env = await setup([known]);
  const response = await post(await request(stranger), env);
  assert.equal(response.status, 403);
  assert.deepEqual(inbox(env), []);
  assert.equal(stub.dispatches(), 0);
});

test("a signature by another key, over another key, or for another audit is refused", async () => {
  stub = githubStub();
  const w = await wallet();
  const other = await wallet();
  const env = await setup([w]);
  assert.equal((await post(await request(w, { signature: await other.sign(statement("001", ENC)) }), env)).status, 400);
  assert.equal((await post(await request(w, { enc_pub: "a".repeat(64) }), env)).status, 400); // signed ENC, sent another
  assert.equal((await post(await request(w, { audit: "002", signature: await w.sign(statement("002", ENC)) }), env)).status, 400);
  assert.deepEqual(inbox(env), []);
  assert.equal(stub.dispatches(), 0);
});

test("malformed, oversized, cross-origin and wrong-type requests are refused", async () => {
  stub = githubStub();
  const w = await wallet();
  const env = await setup([w]);
  const good = await request(w);
  assert.equal((await post("not json", env)).status, 400);
  assert.equal((await post({ wallet: 1 }, env)).status, 400);
  assert.equal((await post({ ...good, enc_pub: "xyz" }, env)).status, 400);
  assert.equal((await post({ ...good, pad: "x".repeat(3000) }, env)).status, 413);
  assert.equal((await post(good, env, { "Content-Type": "text/plain" })).status, 415);
  assert.equal((await post(good, env, { Origin: "https://evil.example" })).status, 403);
  assert.equal((await post(good, env, {}, "/draft/00000000-0000-4000-8000-000000000000/access-request")).status, 404);
  assert.equal((await post(good, env, {}, "/draft/not-a-uuid/access-request")).status, 404);
  assert.deepEqual(inbox(env), []);
});

test("a draft with no known list keeps nothing", async () => {
  stub = githubStub();
  const w = await wallet();
  const { env } = await appEnv({ [`draft/${UUID}/status.json`]: JSON.stringify({ review: { audit: "001" } }) });
  assert.equal((await post(await request(w), env)).status, 403);
  assert.deepEqual(inbox(env), []);
});

test("a submitted review also starts the sync job", async () => {
  stub = githubStub();
  const w = await wallet();
  const message = "ratiaudit review v1\naudit: 001";
  const status = JSON.stringify({ uuid: UUID, review: { audit: "001", message, reviewers: [w.address] }, known: [] });
  const { env } = await appEnv({ [`draft/${UUID}/status.json`]: status });
  const response = await post({ wallet: w.address, message, signature: await w.sign(message) }, env, {}, `/draft/${UUID}/review`);
  assert.equal(response.status, 200);
  assert.equal(stub.dispatches(), 1);
});

test("a request still succeeds when the job cannot be started", async () => {
  stub = githubStub({ failDispatch: true });
  const w = await wallet();
  const env = await setup([w]);
  assert.equal((await post(await request(w), env)).status, 200);
  assert.equal(inbox(env).length, 1); // kept; the next ten-minute tick starts the job
});
