// trust.rati.chat: RATi audits, read from the private R2 bucket `rati-audits` (binding AUDITS).
//
// A draft (/draft/<uuid>) is a status page that changes while an audit is under
// way: counts, open gates, reviewer wallets and a commitment to the record. It
// never holds code, a repository or finding text; the report sits beside it as a
// sealed vault that opens only for the reviewers' Solana wallets.
//
// A final (/<address>) is one immutable bundle: the plain report, its signed
// attestation and a manifest of file hashes. The address is an Arweave
// transaction id (43 characters) or, before upload, the SHA-256 of the manifest.
// Every file's hash is recomputed on each read, so a reader sees whether the
// files still match.
//
// Objects are written by `ratiaudit upload` (atimics/ratiaudit), never by this
// code. Any path that is not an audit path returns null and is proxied on.

import { notify } from "./trigger.js";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
// An Arweave transaction id is 43 base64url characters.
const BUNDLE = /^(?:[0-9a-f]{64}|[A-Za-z0-9_-]{43})$/;
const FINAL_FILES = {
  "record.json": "application/json",
  "report.md": "text/markdown",
  "design.md": "text/markdown",
  "costs.md": "text/markdown",
  "attestation.json": "application/json",
  "attestation.json.sig": "text/plain",
  "allowed_signers": "text/plain",
  "manifest.json": "application/json",
};

// The vault page loads tweetnacl from a CDN and runs inline scripts, and its
// report frame inherits this policy. It may also post a signed review back to
// this site (connect-src 'self'). Nothing else here allows a script.
export const VAULT_CSP =
  "default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com; " +
  "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; " +
  "img-src data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'";
export const PAGE_CSP =
  "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";


const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const WALLET = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const MAX_REVIEW_BYTES = 4096;
const MAX_REQUEST_BYTES = 2048;
const HEX64 = /^[0-9a-f]{64}$/;
const TRUST_ORIGIN = "https://trust.rati.chat";

// Base58 to exactly `length` bytes, or null.
export function b58decode(text, length) {
  if (typeof text !== "string" || !text.length) return null;
  const bytes = [0];
  for (const char of text) {
    const value = ALPHABET.indexOf(char);
    if (value < 0) return null;
    let carry = value;
    for (let i = 0; i < bytes.length; i++) {
      carry += bytes[i] * 58;
      bytes[i] = carry & 0xff;
      carry >>= 8;
    }
    while (carry) {
      bytes.push(carry & 0xff);
      carry >>= 8;
    }
  }
  for (const char of text) {
    if (char !== "1") break;
    bytes.push(0);
  }
  const out = Uint8Array.from(bytes.reverse());
  return out.length === length ? out : null;
}

async function verifyEd25519(wallet, signature, message) {
  const publicKey = b58decode(wallet, 32);
  const sig = b58decode(signature, 64);
  if (!publicKey || !sig) return false;
  try {
    const key = await crypto.subtle.importKey("raw", publicKey, { name: "Ed25519" }, false, ["verify"]);
    return await crypto.subtle.verify({ name: "Ed25519" }, key, sig, new TextEncoder().encode(message));
  } catch {
    return false;
  }
}

const enrolStatement = (audit, encPub) => `ratiaudit enrol v1\naudit: ${audit}\nencryption key: ${encPub}`;

const reply = (request, status, body) =>
  respond(request, JSON.stringify(body), { status, type: "application/json; charset=utf-8" });

// A reviewer submits the review they signed on the sealed page. Nothing is recorded as a review here:
// the object goes to an inbox, and the auditor's `ratiaudit review pull` checks it again and records it.
async function submitReview(request, uuid, env) {
  const origin = request.headers.get("Origin");
  if (origin && origin !== TRUST_ORIGIN) return reply(request, 403, { error: "Cross-origin submissions are refused." });
  if ((request.headers.get("Content-Type") || "").split(";")[0].trim() !== "application/json") {
    return reply(request, 415, { error: "Send JSON." });
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).length > MAX_REVIEW_BYTES) return reply(request, 413, { error: "Too large." });
  let review;
  try {
    review = JSON.parse(text);
  } catch {
    return reply(request, 400, { error: "That is not JSON." });
  }
  const { wallet, message, signature } = review ?? {};
  if (![wallet, message, signature].every((v) => typeof v === "string") || !WALLET.test(wallet)) {
    return reply(request, 400, { error: "Expected wallet, message and signature." });
  }
  const statusObject = await env.AUDITS.get(`draft/${uuid}/status.json`);
  if (!statusObject) return reply(request, 404, { error: "No such audit." });
  let status;
  try {
    status = JSON.parse(await statusObject.text());
  } catch {
    return reply(request, 404, { error: "No such audit." });
  }
  const allowed = status.review?.reviewers;
  if (!Array.isArray(allowed) || !allowed.includes(wallet)) {
    return reply(request, 403, { error: "This wallet is not a reviewer of this audit." });
  }
  if (message !== status.review?.message) {
    return reply(request, 409, { error: "The statement has changed since you signed. Reload the page and sign again." });
  }
  if (!(await verifyEd25519(wallet, signature, message))) {
    return reply(request, 400, { error: "The signature does not match this wallet." });
  }
  await env.AUDITS.put(`inbox/${uuid}/${wallet}.json`, JSON.stringify({ wallet, message, signature }), {
    httpMetadata: { contentType: "application/json" },
  });
  await notify(env);
  return reply(request, 200, { ok: true });
}

// A wallet the auditor already listed asks to be let in. Only a known wallet (the draft publishes the list
// as hashes) with a valid signature on the enrolment statement is kept, in the inbox; the sync job checks it
// again, adds the wallet and seals the report for it. Any other wallet is turned away and stores nothing.
async function submitAccessRequest(request, uuid, env) {
  const origin = request.headers.get("Origin");
  if (origin && origin !== TRUST_ORIGIN) return reply(request, 403, { error: "Cross-origin submissions are refused." });
  if ((request.headers.get("Content-Type") || "").split(";")[0].trim() !== "application/json") {
    return reply(request, 415, { error: "Send JSON." });
  }
  const body = await request.text();
  if (new TextEncoder().encode(body).length > MAX_REQUEST_BYTES) return reply(request, 413, { error: "Too large." });
  let enrolment;
  try {
    enrolment = JSON.parse(body);
  } catch {
    return reply(request, 400, { error: "That is not JSON." });
  }
  const { audit, wallet, enc_pub: encPub, signature } = enrolment ?? {};
  if (![audit, wallet, encPub, signature].every((v) => typeof v === "string") || !WALLET.test(wallet) || !HEX64.test(encPub)) {
    return reply(request, 400, { error: "Expected audit, wallet, enc_pub and signature." });
  }
  const statusObject = await env.AUDITS.get(`draft/${uuid}/status.json`);
  if (!statusObject) return reply(request, 404, { error: "No such audit." });
  let status;
  try {
    status = JSON.parse(await statusObject.text());
  } catch {
    return reply(request, 404, { error: "No such audit." });
  }
  if (!Array.isArray(status.known) || !status.known.includes(await sha256Hex(new TextEncoder().encode(wallet)))) {
    return reply(request, 403, { error: "This wallet is not on the list for this audit." });
  }
  if (audit !== status.review?.audit) return reply(request, 400, { error: "That request is for another audit." });
  if (!(await verifyEd25519(wallet, signature, enrolStatement(audit, encPub)))) {
    return reply(request, 400, { error: "The signature does not match this wallet." });
  }
  await env.AUDITS.put(`inbox/${uuid}/enrol-${wallet}.json`, JSON.stringify({ audit, wallet, enc_pub: encPub, signature }), {
    httpMetadata: { contentType: "application/json" },
  });
  await notify(env);
  return reply(request, 200, { ok: true });
}

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const short = (digest) => (digest && digest.length > 12 ? `${digest.slice(0, 8)}…${digest.slice(-4)}` : esc(digest));

async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function respond(request, body, { status = 200, type = "text/html; charset=utf-8", csp = PAGE_CSP, cache = "no-store" } = {}) {
  return new Response(request.method === "HEAD" ? null : body, {
    status,
    headers: {
      "Content-Type": type,
      "Content-Security-Policy": csp,
      "Cache-Control": cache,
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "Referrer-Policy": "no-referrer",
      "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    },
  });
}

const notFound = (request) => respond(request, "Not found", { status: 404, type: "text/plain; charset=utf-8" });

const STYLE = `
:root{--bg:#f6f6f3;--fg:#1c1f24;--muted:#5d6570;--line:#dcdcd5;--surface:#fff;--ok:#2f7a55;--bad:#b3261e;--accent:#1f4f8f}
@media (prefers-color-scheme:dark){:root{--bg:#14171b;--fg:#e6e8ea;--muted:#9aa3ad;--line:#2f353c;--surface:#1c2025;--ok:#6fcf9b;--bad:#ff8a80;--accent:#7fb0ee;color-scheme:dark}}
body{background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,sans-serif;margin:0;padding:32px 16px}
main{max-width:760px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
h1{font-size:28px;line-height:1.15;margin:4px 0}h2{font-size:14px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 8px}
p{margin:0 0 8px;max-width:64ch}.eyebrow{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
a{color:var(--accent)}code{font:12.5px ui-monospace,Menlo,monospace;overflow-wrap:anywhere}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.cell{background:var(--surface);border:1px solid var(--line);border-radius:6px;padding:10px 12px}
.cell span{display:block;font-size:12px;color:var(--muted)}.cell strong{font-size:17px}
.ok{color:var(--ok)}.bad{color:var(--bad)}.note{color:var(--muted);font-size:13.5px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:6px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:500}tr:last-child td{border-bottom:0}ul{margin:0;padding-left:20px}`;

const shell = (title, body) =>
  `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>${esc(title)}</title><style>${STYLE}</style></head><body><main>${body}</main></body></html>`;

const cell = (label, value) => `<div class="cell"><span>${esc(label)}</span><strong>${value}</strong></div>`;

function draftPage(uuid, status, hasVault) {
  const f = status.findings || {};
  const gates = status.gates || [];
  const reviewers = status.reviewers || [];
  const disclosure = status.disclosure || {};
  const grid = [
    cell("Findings", esc(f.total ?? 0)),
    cell("Open", esc(f.open ?? 0)),
    cell("Fixed", esc(f.fixed ?? 0)),
    cell("Acknowledged by the client", esc(f.acknowledged ?? 0)),
    cell("Gates still open", esc(gates.length)),
    cell("Last checked", esc(status.checked)),
    cell("Protocol", esc(status.protocol?.version)),
    cell("Record commitment", `<code title="${esc(status.record_sha256)}">${short(status.record_sha256)}</code>`),
  ].join("");
  const gateList = gates.length
    ? `<section><h2>Before this audit can be final</h2><ul>${gates.map((g) => `<li>${esc(g)}</li>`).join("")}</ul></section>`
    : "";
  const reviewerRows = reviewers.length
    ? `<div class="wrap"><table><thead><tr><th>Solana wallet</th><th>Findings reviewed</th><th>Review</th></tr></thead><tbody>${reviewers
        .map((r) => `<tr><td><code>${esc(r.wallet)}</code></td><td>${esc((r.findings || []).join(", "))}</td><td>${r.counts ? "counts" : "stale"}</td></tr>`)
        .join("")}</tbody></table></div>`
    : "<p>No review has been recorded yet.</p>";
  const vault = hasVault
    ? `<section><h2>The report</h2><p>The report is encrypted for the reviewers' wallets. Open it in a normal browser tab with Phantom or Solflare.</p><p><a href="/draft/${esc(uuid)}/vault">Open the sealed report</a></p></section>`
    : "";
  const terms =
    disclosure.policy === "public-after-fix"
      ? `<strong>public-after-fix</strong>, up to ${esc(disclosure.max_wait_days)} days for the client to fix`
      : `<strong>${esc(disclosure.policy)}</strong>`;
  return shell(
    "RATi Audit, in progress",
    `<header><div class="eyebrow">RATi Trust · Audit draft</div><h1>An audit in progress</h1>
<p>This page changes until the audit is final. It shows how far the audit has got, never the code, the repository or what was found. The report stays sealed and opens only for the reviewers' wallets.</p></header>
<section><div class="grid">${grid}</div></section>${gateList}
<section><h2>Reviewers</h2>${reviewerRows}</section>${vault}
<p class="note">Disclosure terms: ${terms}. The commitment above is the SHA-256 of the current record; the final audit is published once every finding is fixed or acknowledged.</p>
<p class="note"><a href="/draft/${esc(uuid)}/status.json">status.json</a></p>`,
  );
}

function finalPage(id, bundle) {
  const a = bundle.attestation;
  const m = bundle.manifest;
  const findings = a?.findings
    ? cell("Findings", `${esc(a.findings.total)} (${esc(a.findings.fixed)} fixed, ${esc(a.findings.acknowledged ?? 0)} acknowledged)`)
    : "";
  const grid = [
    cell("Files", bundle.verified ? '<span class="ok">Match their hashes</span>' : '<span class="bad">Do not match their hashes</span>'),
    cell("Signed by", esc(m.signer)),
    cell("Signed on", esc(m.signed)),
    cell("Protocol", esc(m.protocol?.version)),
    findings,
    cell("Bundle hash", `<code title="${esc(bundle.bundleHash)}">${short(bundle.bundleHash)}</code>`),
  ].join("");
  const rows = bundle.files
    .map(
      (f) =>
        `<tr><td><a href="/${esc(id)}/${esc(f.name)}">${esc(f.name)}</a></td><td><code title="${esc(f.sha256)}">${short(f.sha256)}</code></td><td>${
          f.ok ? '<span class="ok">matches</span>' : '<span class="bad">does not match</span>'
        }</td></tr>`,
    )
    .join("");
  return shell(
    `RATi Audit ${m.audit ?? ""}`,
    `<header><div class="eyebrow">RATi Trust · Final audit</div><h1>${esc(a?.title ?? `Audit ${m.audit ?? ""}`)}</h1>
<p>A final audit, signed by a named auditor. Nothing on this page changes.</p></header>
<section><div class="grid">${grid}</div></section>
<section><h2>Files</h2><div class="wrap"><table><thead><tr><th>File</th><th>SHA-256</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></section>
<section><h2>Verify it yourself</h2><p>Download the files and <a href="/${esc(id)}/allowed_signers">allowed_signers</a>, then run <code>ratiaudit check &lt;folder&gt; --allowed-signers allowed_signers</code>. It checks the SSH signature on the attestation and every file hash. Trust the signer's key from a source you already know; this page cannot vouch for it.</p>
<p class="note">The report is <a href="/${esc(id)}/report.md">report.md</a>. Address: <code>${esc(id)}</code></p></section>`,
  );
}

async function loadBundle(id, env) {
  const manifestObject = await env.AUDITS.get(`final/${id}/manifest.json`);
  if (!manifestObject) return null;
  const manifestText = await manifestObject.text();
  let manifest;
  try {
    manifest = JSON.parse(manifestText);
  } catch {
    return null;
  }
  let verified = true;
  const files = [];
  for (const [name, expected] of Object.entries(manifest.files || {}).sort()) {
    let ok = false;
    if (Object.hasOwn(FINAL_FILES, name)) {
      const object = await env.AUDITS.get(`final/${id}/${name}`);
      ok = Boolean(object) && (await sha256Hex(await object.arrayBuffer())) === expected;
    }
    verified &&= ok;
    files.push({ name, sha256: expected, ok });
  }
  let attestation = null;
  try {
    attestation = JSON.parse(await (await env.AUDITS.get(`final/${id}/attestation.json`)).text());
  } catch {
    verified = false;
  }
  return { manifest, attestation, files, verified, bundleHash: await sha256Hex(new TextEncoder().encode(manifestText)) };
}

// Returns a Response for an audit path, or null for anything else.
export async function handleAudits(request, env) {
  const parts = new URL(request.url).pathname.split("/").slice(1);
  if (request.method === "POST" && parts.length === 3 && parts[0] === "draft") {
    if (parts[2] === "review") return UUID.test(parts[1]) ? submitReview(request, parts[1], env) : notFound(request);
    if (parts[2] === "access-request") return UUID.test(parts[1]) ? submitAccessRequest(request, parts[1], env) : notFound(request);
  }
  if (request.method !== "GET" && request.method !== "HEAD") return null;

  if (parts[0] === "draft") {
    const [, uuid, leaf, ...extra] = parts;
    const known = leaf === undefined || leaf === "status.json" || leaf === "vault";
    if (!uuid || !UUID.test(uuid) || extra.length || !known) return notFound(request);
    if (leaf === "status.json") {
      const object = await env.AUDITS.get(`draft/${uuid}/status.json`);
      return object ? respond(request, await object.text(), { type: "application/json; charset=utf-8" }) : notFound(request);
    }
    if (leaf === "vault") {
      const object = await env.AUDITS.get(`draft/${uuid}/vault.html`);
      return object ? respond(request, object.body, { csp: VAULT_CSP }) : notFound(request);
    }
    const object = await env.AUDITS.get(`draft/${uuid}/status.json`);
    if (!object) return notFound(request);
    let status;
    try {
      status = JSON.parse(await object.text());
    } catch {
      return notFound(request);
    }
    const hasVault = Boolean(await env.AUDITS.head(`draft/${uuid}/vault.html`));
    return respond(request, draftPage(uuid, status, hasVault));
  }

  const [id, name, ...extra] = parts;
  if (!id || !BUNDLE.test(id)) return null;
  if (extra.length) return notFound(request);
  if (name === undefined) {
    const bundle = await loadBundle(id, env);
    return bundle ? respond(request, finalPage(id, bundle), { cache: "public, max-age=300" }) : notFound(request);
  }
  if (!Object.hasOwn(FINAL_FILES, name)) return notFound(request);
  const object = await env.AUDITS.get(`final/${id}/${name}`);
  return object
    ? respond(request, await object.arrayBuffer(), { type: `${FINAL_FILES[name]}; charset=utf-8`, cache: "public, max-age=300" })
    : notFound(request);
}
