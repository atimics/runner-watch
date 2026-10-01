// Shared by the trigger and access-request tests: a bucket that enforces compare-and-swap,
// a stand-in wallet, and a stand-in for the GitHub API.

const ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

export function b58encode(bytes) {
  const digits = [0];
  for (const byte of bytes) {
    let carry = byte;
    for (let i = 0; i < digits.length; i++) {
      carry += digits[i] << 8;
      digits[i] = carry % 58;
      carry = (carry / 58) | 0;
    }
    while (carry) {
      digits.push(carry % 58);
      carry = (carry / 58) | 0;
    }
  }
  let out = "";
  for (const byte of bytes) {
    if (byte !== 0) break;
    out += "1";
  }
  for (let i = digits.length - 1; i >= 0; i--) out += ALPHABET[digits[i]];
  return out;
}

export async function wallet() {
  const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
  const address = b58encode(new Uint8Array(await crypto.subtle.exportKey("raw", pair.publicKey)));
  const sign = async (text) => b58encode(new Uint8Array(await crypto.subtle.sign({ name: "Ed25519" }, pair.privateKey, new TextEncoder().encode(text))));
  return { address, sign };
}

export const sha256 = async (text) =>
  [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)))].map((b) => b.toString(16).padStart(2, "0")).join("");

export class CasBucket {
  constructor(objects = {}) {
    this.objects = { ...objects };
    this.etags = {};
    this.counter = 0;
  }
  async get(key) {
    if (!(key in this.objects)) return null;
    const value = this.objects[key];
    return {
      etag: this.etags[key] ?? "initial",
      text: async () => value,
      arrayBuffer: async () => new TextEncoder().encode(value).buffer,
      body: new Response(value).body,
    };
  }
  async head(key) {
    return key in this.objects ? { key } : null;
  }
  async put(key, value, options = {}) {
    await Promise.resolve(); // let other callers interleave first; the check and the write below are then atomic, as in R2
    const rule = options.onlyIf;
    const exists = key in this.objects;
    if (rule?.etagMatches && (!exists || (this.etags[key] ?? "initial") !== rule.etagMatches)) return null;
    if (rule?.etagDoesNotMatch === "*" && exists) return null;
    this.objects[key] = value;
    this.etags[key] = `e${++this.counter}`;
    return { key };
  }
}

// Replaces global fetch with a stand-in for the two GitHub calls the trigger makes.
export function githubStub({ failDispatch = false, failToken = false } = {}) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url: String(url), init });
    if (String(url).endsWith("/access_tokens")) {
      return failToken ? new Response("no", { status: 401 }) : new Response(JSON.stringify({ token: "installation-token" }), { status: 201 });
    }
    if (String(url).includes("/dispatches")) return new Response(null, { status: failDispatch ? 422 : 204 });
    return new Response("unexpected", { status: 404 });
  };
  return { calls, dispatches: () => calls.filter((c) => c.url.includes("/dispatches")).length, restore: () => (globalThis.fetch = original) };
}

// An env with the GitHub App configured, with a real RSA key so the JWT can be verified.
export async function appEnv(objects = {}) {
  const pair = await crypto.subtle.generateKey({ name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["sign", "verify"]);
  const der = new Uint8Array(await crypto.subtle.exportKey("pkcs8", pair.privateKey));
  const b64 = btoa(String.fromCharCode(...der)).match(/.{1,64}/g).join("\n");
  return {
    publicKey: pair.publicKey,
    env: {
      AUDITS: new CasBucket(objects),
      GITHUB_APP_ID: "5144854",
      GITHUB_APP_INSTALLATION_ID: "166747447",
      GITHUB_APP_PRIVATE_KEY: `-----BEGIN PRIVATE KEY-----\n${b64}\n-----END PRIVATE KEY-----\n`,
    },
  };
}
