const UPSTREAM_HOST = "runner-watch-ratimics.fly.dev";
const PUBLIC_HOSTS = new Set(["runners.rati.chat", "sports.rati.chat"]);

export default {
  async fetch(request, env) {
    const publicUrl = new URL(request.url);
    if (!PUBLIC_HOSTS.has(publicUrl.hostname)) {
      return new Response("Unknown RATi product host", { status: 404 });
    }
    if (!env.EDGE_PROXY_SECRET) {
      return new Response("Edge routing is not configured", { status: 503 });
    }

    const upstreamUrl = new URL(request.url);
    upstreamUrl.hostname = UPSTREAM_HOST;
    upstreamUrl.protocol = "https:";
    upstreamUrl.port = "";

    const headers = new Headers(request.headers);
    headers.delete("X-Rati-Client-IP");
    headers.delete("X-Rati-Edge-Secret");
    headers.set("X-Forwarded-Host", publicUrl.hostname);
    headers.set("X-Forwarded-Proto", "https");
    headers.set("X-Rati-Client-IP", request.headers.get("CF-Connecting-IP") || "");
    headers.set("X-Rati-Edge-Secret", env.EDGE_PROXY_SECRET);

    const upstreamRequest = new Request(upstreamUrl, {
      method: request.method,
      headers,
      body: request.body,
      redirect: "manual",
    });
    return fetch(upstreamRequest);
  },
};
