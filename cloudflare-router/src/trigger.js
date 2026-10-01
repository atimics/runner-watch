// Starting the "Access sync" workflow in cenetex/ratiaudit when something was submitted, and never
// more than once every ten minutes.
//
// A submission marks work as pending. The first one after a quiet spell starts the workflow at once;
// later ones only keep the mark, and a ten-minute timer starts the workflow when the window has passed.
// The mark and the last start time live in one small object in the private bucket, changed only with
// a compare-and-swap so two requests at the same moment cannot both start it.
//
// The workflow is started as a GitHub App that has Actions: write on that one repository. It can start
// that workflow; it cannot read code or secrets and it cannot push.

export const WINDOW_MS = 10 * 60 * 1000;
const CONTROL = "control/access-sync.json";
const HEARTBEAT = "control/last-tick.json";
const REPO = "cenetex/ratiaudit";
const WORKFLOW = "access-sync.yml";
const API = "https://api.github.com";

const b64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/, "");
const text = (value) => new TextEncoder().encode(value);

function pemToDer(pem) {
  const body = pem.replace(/-----[A-Z ]+-----/g, "").replace(/\s+/g, "");
  return Uint8Array.from(atob(body), (c) => c.charCodeAt(0));
}

// A short-lived token proving we are the app (RS256, as GitHub requires).
export async function appJwt(env, nowMs) {
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToDer(env.GITHUB_APP_PRIVATE_KEY),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const now = Math.floor(nowMs / 1000);
  const head = b64url(text(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const body = b64url(text(JSON.stringify({ iat: now - 30, exp: now + 300, iss: String(env.GITHUB_APP_ID) })));
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, text(`${head}.${body}`));
  return `${head}.${body}.${b64url(signature)}`;
}

const headers = (token) => ({
  Authorization: `Bearer ${token}`,
  Accept: "application/vnd.github+json",
  "X-GitHub-Api-Version": "2022-11-28",
  "User-Agent": "rati-trust-router",
});

export const configured = (env) => Boolean(env.GITHUB_APP_ID && env.GITHUB_APP_INSTALLATION_ID && env.GITHUB_APP_PRIVATE_KEY);

// Starts the workflow. Returns true when GitHub accepted it.
export async function dispatchWorkflow(env, nowMs = Date.now()) {
  const jwt = await appJwt(env, nowMs);
  const issued = await fetch(`${API}/app/installations/${env.GITHUB_APP_INSTALLATION_ID}/access_tokens`, { method: "POST", headers: headers(jwt) });
  if (!issued.ok) return false;
  const { token } = await issued.json();
  const started = await fetch(`${API}/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`, {
    method: "POST",
    headers: { ...headers(token), "Content-Type": "application/json" },
    body: JSON.stringify({ ref: "main" }),
  });
  return started.status === 204;
}

// Change the control object with a compare-and-swap. `change(state)` returns the next state, or null for no change.
async function update(env, change) {
  for (let attempt = 0; attempt < 5; attempt++) {
    const current = await env.AUDITS.get(CONTROL);
    const state = current ? JSON.parse(await current.text()) : {};
    const next = change(state);
    if (next === null) return state;
    const written = await env.AUDITS.put(CONTROL, JSON.stringify(next), {
      onlyIf: current ? { etagMatches: current.etag } : { etagDoesNotMatch: "*" },
      httpMetadata: { contentType: "application/json" },
    });
    if (written) return next;
  }
  return null;
}

// If the window is open and work is pending, take the turn: record the start, then start the workflow.
// A failed start gives the turn back, so the next timer tick tries again. Returns what happened, in words.
async function startIfDue(env, nowMs) {
  let previous = 0;
  let reason = "lost the race";
  const taken = await update(env, (state) => {
    if (!state.pending) {
      reason = "nothing pending";
      return null;
    }
    if (nowMs - (state.lastDispatch || 0) < WINDOW_MS) {
      reason = "waiting for the window";
      return null;
    }
    previous = state.lastDispatch || 0;
    reason = "taken";
    return { pending: false, lastDispatch: nowMs };
  });
  // update() gives up (null) when every compare-and-swap lost: then the turn was never recorded, so it is not ours.
  if (taken === null) return "could not record the turn";
  if (reason !== "taken") return reason;
  let started = false;
  try {
    started = await dispatchWorkflow(env, nowMs);
  } catch {
    started = false;
  }
  if (started) return "started";
  await update(env, (state) => ({ ...state, pending: true, lastDispatch: previous }));
  return "start refused or failed; will retry";
}

// Something was submitted. Never throws: a failure here must not fail the submission.
export async function notify(env, nowMs = Date.now()) {
  if (!env.AUDITS || !configured(env)) return false;
  try {
    await update(env, (state) => (state.pending ? null : { ...state, pending: true }));
    return (await startIfDue(env, nowMs)) === "started";
  } catch {
    return false;
  }
}

// The ten-minute timer: start the workflow if work is waiting and the window has passed.
export async function tick(env, nowMs = Date.now()) {
  if (!env.AUDITS || !configured(env)) return false;
  try {
    return (await startIfDue(env, nowMs)) === "started";
  } catch {
    return false;
  }
}

// What the ten-minute timer runs: the same as tick(), and it always leaves a heartbeat saying what happened,
// so a timer that is not firing, or fires and does nothing, can be told apart from one that works.
export async function runTimer(env, nowMs = Date.now()) {
  let result;
  try {
    if (!env.AUDITS) result = "no bucket binding";
    else if (!configured(env)) result = "GitHub App secrets missing";
    else result = await startIfDue(env, nowMs);
  } catch (error) {
    result = `error: ${error.message}`;
  }
  try {
    await env.AUDITS?.put(HEARTBEAT, JSON.stringify({ at: new Date(nowMs).toISOString(), result }), {
      httpMetadata: { contentType: "application/json" },
    });
  } catch {
    // The heartbeat is a courtesy; it must not make the timer fail.
  }
  return result;
}
