(() => {
  "use strict";
  const root = document.querySelector("[data-token-replay]");
  if (!root) return;
  const $ = name => root.querySelector(`[data-replay-${name}]`);
  const graph = $("graph"), status = $("status"), play = $("play"), position = $("position");
  const ns = "http://www.w3.org/2000/svg";
  const colors = {launch: "#d3eb86", wallet: "#77c9ce", token: "#c8d284", pool: "#b69be4"};
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let data, index = 0, shown, animation = 0, timer = 0, playing = false, generation = 0;
  let loadController, receiptBase;
  const make = (tag, attrs = {}, text) => {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const stop = () => { playing = false; clearTimeout(timer); play.textContent = "Replay events"; };
  function details(id) {
    stop();
    cancelAnimationFrame(animation); generation++;
    const frame = data.frames[index];
    draw(frame);
    const node = frame.nodes.find(n => n.id === id);
    if (!node) return;
    const target = $("selection");
    target.replaceChildren();
    const heading = document.createElement("p");
    heading.textContent = node.kind === "launch" ? `Token launch · ${data.launch ? "recorded on chain" : "evidence pending"}` : `${node.kind} · ${node.address}`;
    target.append(heading);
    const events = new Map(data.events.map(e => [e.event_id, e]));
    for (const edge of frame.edges.filter(e => e.source === id || e.target === id)) {
      const event = events.get(edge.event_id);
      const line = document.createElement("p"), link = document.createElement("a");
      link.href = `${receiptBase}${encodeURIComponent(edge.signature)}`;
      link.textContent = `${edge.role} · slot ${edge.slot} ↗`;
      link.target = "_blank"; link.rel = "noopener noreferrer";
      line.append(link);
      if (event.net_token_amount) line.append(` · ${event.net_token_amount} tokens (${event.amount_basis})`);
      if (event.balances) for (const b of event.balances) line.append(` · ${b.raw} raw units / ${b.decimals} decimals of ${b.mint}`);
      target.append(line);
    }
    root.querySelector(".replay-evidence").open = true;
    graph.querySelector(`[data-node="${CSS.escape(id)}"]`)?.focus();
  }
  function draw(frame) {
    graph.replaceChildren();
    const nodes = new Map(frame.nodes.map(n => [n.id, n]));
    for (const edge of frame.edges) {
      const a = nodes.get(edge.source), b = nodes.get(edge.target);
      if (!a || !b) continue;
      graph.append(make("line", {x1:a.x, y1:a.y, x2:b.x, y2:b.y, stroke:"#507c86", "stroke-width":1,
        opacity:Math.min(a.opacity ?? 1, b.opacity ?? 1)}));
    }
    for (const node of frame.nodes) {
      const group = make("g", {transform:`translate(${node.x} ${node.y})`, role:"button", tabindex:0,
        "aria-label":`${node.kind}: ${node.address}`, opacity:node.opacity ?? 1, "data-node":node.id});
      group.append(make("circle", {r:node.r, fill:colors[node.kind], "fill-opacity":.16, stroke:colors[node.kind], "stroke-width":1.5}));
      group.append(make("text", {"text-anchor":"middle", y:node.id === "launch" ? -4 : 4}, node.id === "launch" ? "Token launch" : node.address.slice(0,4)));
      if (node.id === "launch") group.append(make("text", {"text-anchor":"middle", y:15, class:"replay-origin-state"}, data.launch ? "Recorded on chain" : "Evidence pending"));
      group.addEventListener("click", () => details(node.id));
      group.addEventListener("keydown", event => {if (["Enter", " "].includes(event.key)) {event.preventDefault(); details(node.id);}});
      graph.append(group);
    }
    shown = frame;
  }
  function blend(first, last, progress, reset) {
    const t = progress * progress * (3 - 2 * progress);
    const before = new Map(first.nodes.map(n => [n.id,n])), after = new Map(last.nodes.map(n => [n.id,n]));
    const keys = [...after.keys(), ...(reset ? [...before.keys()].filter(k => !after.has(k)) : [])];
    return {...last, edges:reset ? first.edges : last.edges, nodes:keys.map(key => {
      let start = before.get(key), end = after.get(key);
      if (!start) start = {...(before.get(end.parent) || before.get("launch")), r:0};
      if (!end) end = {...last.nodes[0], r:0};
      const node = {...(after.get(key) || before.get(key)), opacity:!after.has(key) ? 1-t : !before.has(key) ? t : 1};
      for (const name of ["x", "y", "r"]) node[name] = start[name] + (end[name] - start[name]) * t;
      return node;
    })};
  }
  function select(next, {animate = true, reset = false} = {}) {
    cancelAnimationFrame(animation);
    const run = ++generation;
    index = next;
    position.value = String(index);
    const target = data.frames[index], first = shown || target;
    $("time").textContent = `${target.label} · keyframe ${index + 1} of ${data.frames.length}`;
    $("selection").textContent = "Choose a bubble to view its recorded connections.";
    const duration = animate && !motion.matches ? (reset ? data.timing.reset : data.timing.transition) : 0;
    const start = performance.now();
    graph.dataset.phase = reset ? "returning-to-launch" : "transition";
    function tick(now) {
      if (run !== generation) return;
      const t = duration ? Math.min(1, (now-start)/duration) : 1;
      draw(t === 1 ? target : blend(first, target, t, reset));
      if (t < 1) animation = requestAnimationFrame(tick);
      else {
        graph.dataset.phase = "settled";
        if (playing) timer = setTimeout(advance, index === 0 ? data.timing.origin_hold : index === data.frames.length-1 ? data.timing.final_hold : data.timing.hold);
      }
    }
    tick(start);
  }
  function advance() {
    if (!playing) return;
    if (index < data.frames.length - 1) select(index + 1);
    else if ($("loop").checked) select(0, {reset:true});
    else stop();
  }
  play.addEventListener("click", () => {
    if (playing) return stop();
    playing = true; play.textContent = "Pause replay";
    select(index === data.frames.length-1 ? 0 : index, {reset:index === data.frames.length-1});
  });
  $("origin").addEventListener("click", () => {stop(); select(0, {animate:false});});
  $("latest").addEventListener("click", () => {stop(); select(data.frames.length-1);});
  position.addEventListener("input", () => {stop(); select(Number(position.value), {animate:false});});
  document.addEventListener("visibilitychange", () => {if (document.hidden) {stop(); if (data) select(index, {animate:false});}});
  motion.addEventListener("change", () => {if (data) {stop(); select(index, {animate:false});}});
  async function load() {
    loadController?.abort(); loadController = new AbortController();
    const revision = new URL(location.href).searchParams.get("replay");
    const url = `/api/memecoins/${encodeURIComponent(root.dataset.coinId)}/replay${revision ? `?revision=${encodeURIComponent(revision)}` : ""}`;
    try {
      const response = await fetch(url, {signal:loadController.signal, headers:{Accept:"application/json"}});
      if (!response.ok) throw new Error("The saved replay is awaiting evidence review.");
      const record = await response.json();
      if (record.status !== "ready") {status.textContent = record.message; timer = setTimeout(load, 15000); return;}
      data = record.payload;
      receiptBase = record.receipt_base || "/api/memecoins/evidence/";
      const extent = Math.max(190, ...data.frames.at(-1).nodes.flatMap(n => [Math.abs(n.x-400)+n.r+30, Math.abs(n.y-292)+n.r+30]));
      graph.setAttribute("viewBox", `${400-extent} ${292-extent} ${extent*2} ${extent*2}`);
      $("badge").textContent = data.launch ? "Launch recorded" : "Launch evidence pending";
      status.textContent = "Saved chain evidence · replay checks passed";
      $("content").hidden = false;
      position.max = String(data.frames.length-1);
      $("gif").href = record.gif_url; $("evidence").href = record.evidence_url;
      $("coverage").textContent = `${data.coverage.drawn_events} events shown from ${data.coverage.saved_events} saved events · ${data.coverage.drawn_nodes} bubbles · ${data.frames.length} keyframes. ${data.coverage.basis}${record.collection_status ? " Further collection is pending." : ""}`;
      select(0, {animate:false});
    } catch (error) {if (error.name !== "AbortError") status.textContent = error.message;}
  }
  window.addEventListener("pagehide", () => {stop(); loadController?.abort(); cancelAnimationFrame(animation);});
  load();
})();
