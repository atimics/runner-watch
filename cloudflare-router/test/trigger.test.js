import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { appJwt, dispatchWorkflow, notify, runTimer, tick, WINDOW_MS } from "../src/trigger.js";
import { appEnv, CasBucket, githubStub } from "../testing/helpers.js";

let stub;
afterEach(() => stub?.restore());

const T0 = 1_800_000_000_000;
const state = async (env) => JSON.parse((await (await env.AUDITS.get("control/access-sync.json")).text()));

test("the app token is a valid RS256 JWT for the app, short lived", async () => {
  const { env, publicKey } = await appEnv();
  const [head, body, signature] = (await appJwt(env, T0)).split(".");
  const decode = (s) => JSON.parse(atob(s.replaceAll("-", "+").replaceAll("_", "/")));
  assert.deepEqual(decode(head), { alg: "RS256", typ: "JWT" });
  const claims = decode(body);
  assert.equal(claims.iss, "5144854");
  assert.ok(claims.exp - claims.iat <= 600);
  const raw = Uint8Array.from(atob(signature.replaceAll("-", "+").replaceAll("_", "/") + "=".repeat((4 - (signature.length % 4)) % 4)), (c) => c.charCodeAt(0));
  assert.ok(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", publicKey, raw, new TextEncoder().encode(`${head}.${body}`)));
});

test("the workflow is started with the installation token, on main, in the one repository", async () => {
  stub = githubStub();
  const { env } = await appEnv();
  assert.equal(await dispatchWorkflow(env, T0), true);
  const [issue, start] = stub.calls;
  assert.ok(issue.url.endsWith("/app/installations/166747447/access_tokens"));
  assert.match(issue.init.headers.Authorization, /^Bearer eyJ/); // the JWT asks for the token
  assert.equal(start.url, "https://api.github.com/repos/cenetex/ratiaudit/actions/workflows/access-sync.yml/dispatches");
  assert.equal(start.init.headers.Authorization, "Bearer installation-token"); // the token starts the run
  assert.deepEqual(JSON.parse(start.init.body), { ref: "main" });
});

test("a refused token or dispatch is reported, not thrown", async () => {
  const { env } = await appEnv();
  stub = githubStub({ failToken: true });
  assert.equal(await dispatchWorkflow(env, T0), false);
  stub.restore();
  stub = githubStub({ failDispatch: true });
  assert.equal(await dispatchWorkflow(env, T0), false);
});

test("the first submission after a quiet spell starts the workflow at once; later ones wait for the window", async () => {
  stub = githubStub();
  const { env } = await appEnv();
  assert.equal(await notify(env, T0), true);
  assert.equal(stub.dispatches(), 1);
  assert.deepEqual(await state(env), { pending: false, lastDispatch: T0 });

  assert.equal(await notify(env, T0 + 60_000), false); // one minute later: only marked
  assert.equal(stub.dispatches(), 1);
  assert.equal((await state(env)).pending, true);

  assert.equal(await tick(env, T0 + 5 * 60_000), false); // the window has not passed
  assert.equal(stub.dispatches(), 1);
  assert.equal(await tick(env, T0 + WINDOW_MS), true); // now it has
  assert.equal(stub.dispatches(), 2);
  assert.equal((await state(env)).pending, false);
  assert.equal(await tick(env, T0 + 2 * WINDOW_MS), false); // nothing waiting: nothing starts
  assert.equal(stub.dispatches(), 2);
});

test("a burst of submissions at the same moment starts one run", async () => {
  stub = githubStub();
  const { env } = await appEnv();
  await Promise.all([notify(env, T0), notify(env, T0), notify(env, T0), notify(env, T0)]);
  assert.equal(stub.dispatches(), 1);
});

test("a failed start keeps the work pending and the next tick tries again", async () => {
  const { env } = await appEnv();
  stub = githubStub({ failDispatch: true });
  assert.equal(await notify(env, T0), false);
  assert.deepEqual(await state(env), { pending: true, lastDispatch: 0 });
  stub.restore();
  stub = githubStub();
  assert.equal(await tick(env, T0 + 1000), true);
  assert.equal(stub.dispatches(), 1);
});

test("with the app not configured nothing is written and nothing is called", async () => {
  stub = githubStub();
  const env = { AUDITS: new CasBucket() };
  assert.equal(await notify(env, T0), false);
  assert.equal(await tick(env, T0), false);
  assert.equal(stub.calls.length, 0);
  assert.deepEqual(env.AUDITS.objects, {});
});

test("a GitHub outage never makes a submission fail", async () => {
  const { env } = await appEnv();
  globalThis.fetch = async () => {
    throw new Error("network down");
  };
  stub = { restore() {} };
  assert.equal(await notify(env, T0), false);
});

test("if every compare-and-swap loses, nothing is started and nothing throws", async () => {
  stub = githubStub();
  // Work is already pending and the window is open; only taking the turn can fail.
  const { env } = await appEnv({ "control/access-sync.json": JSON.stringify({ pending: true, lastDispatch: 0 }) });
  const original = env.AUDITS.put.bind(env.AUDITS);
  env.AUDITS.put = async (key, value, options) => (options?.onlyIf ? null : original(key, value, options));
  assert.equal(await notify(env, T0), false);
  assert.equal(await tick(env, T0), false);
  assert.equal(stub.dispatches(), 0, "a run was started without recording whose turn it was");
});

const beat = async (env) => JSON.parse(await (await env.AUDITS.get("control/last-tick.json")).text());

test("every timer tick leaves a heartbeat that says what it did", async () => {
  stub = githubStub();
  const { env } = await appEnv();
  assert.equal(await runTimer(env, T0), "nothing pending");
  assert.deepEqual(await beat(env), { at: new Date(T0).toISOString(), result: "nothing pending" });

  await notify(env, T0 + 1000); // starts at once and records the start
  await notify(env, T0 + 2000); // marked pending only
  assert.equal(await runTimer(env, T0 + 60_000), "waiting for the window");
  assert.equal((await beat(env)).result, "waiting for the window");

  assert.equal(await runTimer(env, T0 + 1000 + WINDOW_MS), "started");
  assert.equal(stub.dispatches(), 2);
  assert.equal((await beat(env)).result, "started");
});

test("a refused start is named in the heartbeat and retried on the next tick", async () => {
  const { env } = await appEnv({ "control/access-sync.json": JSON.stringify({ pending: true, lastDispatch: 0 }) });
  stub = githubStub({ failDispatch: true });
  assert.equal(await runTimer(env, T0), "start refused or failed; will retry");
  stub.restore();
  stub = githubStub();
  assert.equal(await runTimer(env, T0 + 1000), "started");
});

test("the heartbeat names a missing configuration and survives errors", async () => {
  const bare = { AUDITS: new CasBucket() };
  assert.equal(await runTimer(bare, T0), "GitHub App secrets missing");
  assert.equal((await beat(bare)).result, "GitHub App secrets missing");
  assert.equal(await runTimer({}, T0), "no bucket binding");
  const { env } = await appEnv({ "control/access-sync.json": "not json" });
  assert.match(await runTimer(env, T0), /^error:/);
});
