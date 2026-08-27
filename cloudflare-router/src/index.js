const UPSTREAM_HOST = "runner-watch-ratimics.fly.dev";
const PUBLIC_HOSTS = new Set(["runners.rati.chat", "sports.rati.chat"]);

export default {
  async fetch(request) {
    const publicUrl = new URL(request.url);
    if (!PUBLIC_HOSTS.has(publicUrl.hostname)) {
      return new Response("Unknown RATi product host", { status: 404 });
    }

    const upstreamUrl = new URL(request.url);
    upstreamUrl.hostname = UPSTREAM_HOST;
    upstreamUrl.protocol = "https:";
    upstreamUrl.port = "";

    const headers = new Headers(request.headers);
    headers.set("X-Forwarded-Host", publicUrl.hostname);
    headers.set("X-Forwarded-Proto", "https");

    const upstreamRequest = new Request(upstreamUrl, {
      method: request.method,
      headers,
      body: request.body,
      redirect: "manual",
    });
    return fetch(upstreamRequest);
  },
};
