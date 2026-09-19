(() => {
  'use strict';
  const root = document.querySelector('[data-stock-map]');
  if (!root) return;
  const $ = key => root.querySelector(`[data-map-${key}]`);
  const ns = 'http://www.w3.org/2000/svg';
  const make = (tag, text, cls) => {
    const el = document.createElement(tag); if (text != null) el.textContent = text;
    if (cls) el.className = cls; return el;
  };
  const svg = (tag, attrs, text) => {
    const el = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k,v));
    if (text != null) el.textContent = text; return el;
  };
  const number = value => value == null ? 'See filing' : Number(value).toLocaleString('en-US', {maximumFractionDigits:6});
  const money = value => value == null ? 'See filing' : '$' + number(value);
  const date = value => {
    if (!value) return 'See filing';
    const time = new Date(value);
    return Number.isFinite(time.getTime()) ? time.toLocaleDateString('en-US', {year:'numeric',month:'short',day:'numeric',timeZone:'UTC'}) : 'See filing';
  };
  const names = e => e.people.map(p => p.name).join(' + ') || 'Reporting person';
  const amount = e => e.view === 'ownership' ? (e.percent == null ? 'See filing' : number(e.percent) + '% of class') : money(e.value);
  const screenNode = document.getElementById('screenData');
  const initial = screenNode?.ratiScreenDetail || JSON.parse(screenNode?.textContent || '{}');
  let item = initial.market === 'stocks' && initial.item?.id === root.dataset.ticker ? initial.item : {};
  let drivers = [], positive = [], penalties = [], contributions = [], total = 0, score = '—';
  const scoreData = value => JSON.stringify([value.score ?? null, value.score_detail ?? null]);
  function readScore() {
    const parts = value => Array.isArray(value) ? value.filter(part => part && Number.isFinite(part.value)) : [];
    drivers = parts(item.score_detail?.drivers);
    positive = drivers.filter(part => part.value > 0);
    penalties = [...parts(item.score_detail?.penalties), ...drivers.filter(part => part.value < 0)].filter(part => part.value !== 0).map(part => ({...part, value:-Math.abs(part.value)}));
    contributions = [...positive, ...penalties];
    total = contributions.reduce((sum, part) => sum + Math.abs(part.value), 0);
    score = Number.isFinite(item.score) ? number(Math.round(item.score)) : '—';
  }
  readScore();
  const points = value => `${value > 0 ? '+' : ''}${number(value)} pts`;
  const percent = part => `${number(Math.round(Math.abs(part.value) / total * 1000) / 10)}% of total magnitude`;
  const color = part => part.value < 0 ? 'var(--red)' : `var(--score-${part.key}, var(--muted))`;
  const metrics = () => small.matches ? {cx:180, cy:184, radius:48, width:16} : {cx:380, cy:218, radius:62, width:20};
  const graph = $('graph');
  const peopleLayer = svg('g', {class:'map-people'});
  const ringLayer = svg('g', {class:'map-ring'});
  const interestsLayer = svg('g', {class:'map-interests'});
  graph.append(peopleLayer, interestsLayer, ringLayer);
  let pinned = null, hovered = null, focused = null;
  let events = [], cursor = null, selected = null;
  let cutoff = Infinity, page = 0, pending = false, filingsPageNumber = 0;
  let ringDirty = true, scene = [], animation = 0, generation = 0;
  const small = window.matchMedia('(max-width:500px)');
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const pageSize = () => small.matches ? 4 : 8;
  const FILINGS_PAGE_SIZE = 5;
  const ANIMATION_MS = 320;
  const subset = () => events.filter(e => (Date.parse(e.filed_at) || 0) <= cutoff);
  function scorePanel(part) {
    const panel = $('selection'); panel.replaceChildren();
    if (part) {
      panel.append(make('h4', part.label), make('p', `${points(part.value)} · ${percent(part)}`, 'map-score-breakdown'));
      panel.append(make('p', pinned === part.key ? 'Pinned contribution. Return to score overview to clear.' : 'Click or press Enter to pin this contribution.', 'map-note'));
    }
    const badges = make('div', null, 'map-score-badges');
    const legend = make('ul', null, 'map-score-legend');
    positive.forEach(driver => {
      const row = make('li');
      const swatch = make('span', null, 'map-score-swatch'); swatch.style.background = color(driver); swatch.setAttribute('aria-hidden', 'true');
      row.append(swatch, make('span', driver.label), make('strong', points(driver.value))); legend.append(row);
    });
    if (legend.children.length) badges.append(legend);
    if (penalties.length) {
      const list = make('ul', null, 'map-score-penalties');
      penalties.forEach(penalty => {const row = make('li'); row.append(make('span', penalty.label), make('strong', points(penalty.value))); list.append(row);});
      badges.append(list);
    }
    if (badges.children.length) panel.append(badges);
  }
  function context() {
    source(subset().find(event => event.id === selected));
  }
  function overview() {
    selected = null; pinned = null; hovered = null; focused = null; ringDirty = true;
    render(false, false);
    graph.querySelector('[data-map-score-center]')?.focus({preventScroll:true});
  }
  function drawRing() {
    ringLayer.replaceChildren();
    const {cx, cy, radius, width} = metrics();
    ringLayer.append(svg('circle', {cx, cy, r:radius, class:'map-score-track', 'stroke-width':width}));
    let angle = -Math.PI / 2;
    contributions.forEach(part => {
      const sweep = Math.abs(part.value) / total * Math.PI * 2, end = angle + sweep;
      const attrs = {class:'map-score-segment', role:'button', tabindex:0, 'data-score-key':part.key, 'aria-label':`${part.label}: ${points(part.value)}, ${percent(part)}`, 'aria-pressed':String(pinned === part.key), 'stroke-width':width};
      const segment = contributions.length === 1 ? svg('circle', {...attrs, cx, cy, r:radius}) : svg('path', {...attrs, d:`M ${cx + radius * Math.cos(angle)} ${cy + radius * Math.sin(angle)} A ${radius} ${radius} 0 ${sweep > Math.PI ? 1 : 0} 1 ${cx + radius * Math.cos(end)} ${cy + radius * Math.sin(end)}`});
      segment.style.stroke = color(part); angle = end;
      segment.append(svg('title', {}, `${part.label}: ${points(part.value)} · ${percent(part)}`));
      segment.addEventListener('mouseenter', () => {hovered = part; context();});
      segment.addEventListener('mouseleave', () => {hovered = null; context();});
      segment.addEventListener('focus', () => {focused = part; context();});
      segment.addEventListener('blur', () => {focused = null; context();});
      const activate = () => {
        pinned = part.key; selected = null; hovered = null; focused = null; ringDirty = true;
        render(false, false);
        ringLayer.querySelector(`[data-score-key="${CSS.escape(part.key)}"]`)?.focus({preventScroll:true});
      };
      segment.addEventListener('click', activate);
      segment.addEventListener('keydown', event => {
        if (['Enter', ' '].includes(event.key)) {event.preventDefault(); activate();}
        if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          event.preventDefault();
          const segments = [...ringLayer.querySelectorAll('.map-score-segment')], index = segments.indexOf(segment);
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? segments.length - 1 : (index + (['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1) + segments.length) % segments.length;
          segments[next].focus({preventScroll:true});
        }
      });
      ringLayer.append(segment);
    });
    const center = svg('g', {role:'button', tabindex:0, class:'map-score-center', 'data-map-score-center':'', 'aria-label':`${root.dataset.ticker}, current score ${score}. Show score overview.`});
    center.append(svg('circle', {cx, cy, r:radius - width / 2 - 3, class:'map-center'}), svg('text', {x:cx, y:cy - 6, 'text-anchor':'middle', class:'map-center-text'}, root.dataset.ticker), svg('text', {x:cx, y:cy + 16, 'text-anchor':'middle', class:'map-center-score'}, score));
    center.addEventListener('click', overview);
    center.addEventListener('keydown', event => {if (['Enter', ' '].includes(event.key)) {event.preventDefault(); overview();}});
    ringLayer.append(center);
  }
  function source(event) {
    const preview = hovered || focused;
    root.querySelector('.map-workspace').classList.toggle('map-overview', !event);
    const panel = $('selection');
    $('score-return').hidden = !event && !pinned;
    if (preview || !event) {
      scorePanel(preview || contributions.find(part => part.key === pinned));
      if (!preview) document.dispatchEvent(new CustomEvent('rati:map-time', {detail:{time:null}})); return;
    }
    panel.replaceChildren();
    panel.append(make('h3', names(event)), make('p', `${event.action} · ${amount(event)}`, 'map-event-action'));
    panel.append(make('p', event.people.map(p => p.role).join(' · ')));
    const facts = make('dl');
    const entries = [[event.view === 'ownership' ? 'As of' : 'Trade date',date(event.occurred_at)],
      ['Filed',date(event.filed_at)], ['Collected',date(event.collected_at)],
      ['Shares / units',number(event.shares)], ['Share class',event.security || 'See filing'],
      ['Evidence',event.basis], ['Form',event.form + (event.amendment ? ' · Amendment' : '')]];
    if (event.view === 'activity') entries.splice(4,0,['Price per unit',money(event.price)]);
    if (event.code) entries.push(['SEC code',event.code]);
    if (event.direction) entries.push(['Direction',event.direction === 'A' ? 'Acquired' : 'Disposed']);
    if (event.ownership) entries.push(['Holding',event.ownership === 'D' ? 'Direct' : 'Indirect']);
    if (event.post_shares != null) entries.push(['After transaction',number(event.post_shares)]);
    if (event.security_type === 'derivative') entries.push(['Security type','Derivative'], ['Underlying',event.underlying_security || 'See filing']);
    entries.forEach(([key,value]) => {const row = make('div'); row.append(make('dt',key),make('dd',value)); facts.append(row);});
    panel.append(facts);
    if (event.joint) panel.append(make('p','Joint report. The source describes how the reporting people share this interest.'));
    if (event.amendment) panel.append(make('p','This amendment has its own filing date. Open the source to see the correction and its scope.'));
    if (event.people.some(p => p.identity_basis === 'Reported name')) panel.append(make('p','Identity matched by the reported name within this ticker.'));
    if (event.footnotes || event.ownership_detail) {
      panel.append(make('p',[event.footnotes,event.ownership_detail].filter(Boolean).join(' '),'map-filing-notes'));
    }
    if (event.source_url) {
      const link = make('a','Open SEC filing ↗'); link.href = event.source_url; link.target = '_blank'; link.rel = 'noopener noreferrer'; panel.append(link);
    }
    document.dispatchEvent(new CustomEvent('rati:map-time', {detail:{time:event.filed_at,label:`Filed ${date(event.filed_at)}`}}));
  }
  function peopleFor(rows) {
    const people = new Map();
    rows.forEach(e => e.people.forEach(p => {
      if (!people.has(p.id)) people.set(p.id,{...p,events:[]});
      people.get(p.id).events.push(e);
    }));
    return [...people.values()];
  }
  function buildScene(event) {
    const size = pageSize();
    const all = peopleFor(subset());
    const shown = all.slice(page*size, page*size + size);
    const magnitude = event => event.view === 'ownership' ? event.percent : event.value;
    const maxAmount = view => Math.max(0, ...all.map(p => p.events[0]).filter(e => e.view === view).map(e => finiteAmount(magnitude(e)) ? magnitude(e) : 0));
    return shown.map((person, i) => {
      const left = i % 2 === 0;
      const x = small.matches ? (left ? 85 : 275) : (left ? 180 : 580);
      const y = small.matches ? 64 + Math.floor(i/2)*210 : 62 + Math.floor(i/2)*103;
      const first = person.events[0];
      const tones = new Set(person.events.map(e => e.tone));
      return {id:person.id, name:person.name, first, eventCount:person.events.length, radius:finiteAmount(magnitude(first)) ? Math.sqrt(144 + 640 * (maxAmount(first.view) ? magnitude(first)/maxAmount(first.view) : 0)) : 16, tone:tones.size === 1 ? first.tone : 'neutral', active:!!event?.people.some(p => p.id === person.id), x, y};
    });
  }
  function drawScene(nodes) {
    peopleLayer.replaceChildren();
    const {cx, cy} = metrics();
    nodes.forEach(node => {
      const opacity = node.opacity ?? 1;
      const line = svg('line', {x1:cx, y1:cy, x2:node.x, y2:node.y, class:`map-edge ${node.tone}`, 'stroke-opacity':opacity});
      line.addEventListener('click', () => choose(node.first, node.id)); peopleLayer.append(line);
      const g = svg('g', {class:`map-person ${node.tone}`, role:'button', tabindex:0, 'aria-label':`${node.name}: ${node.first.action}`, 'aria-pressed':String(!!node.active), 'data-person':node.id, opacity});
      g.append(svg('circle', {cx:node.x, cy:node.y, r:node.radius}));
      const initials = node.name.split(/\s+/).slice(0,2).map(n => n[0]).join('');
      g.append(svg('text', {x:node.x, y:node.y+5, 'text-anchor':'middle'}, initials));
      const maxName = small.matches ? 21 : 28;
      g.append(svg('text', {x:node.x, y:node.y+42, 'text-anchor':'middle'}, node.name.length > maxName ? node.name.slice(0,maxName-2)+'…' : node.name));
      const action = small.matches ? ({'Exercise or conversion':'Exercise / conversion','Tax or exercise payment':'Tax / exercise payment'}[node.first.action] || node.first.action) : `${node.first.action} · ${amount(node.first)}`;
      g.append(svg('text', {x:node.x, y:node.y+59, 'text-anchor':'middle', class:'map-node-action'}, action));
      g.append(svg('title', {}, node.name));
      const activate = () => {choose(node.first, node.id); peopleLayer.querySelector(`[data-person="${CSS.escape(node.id)}"]`)?.focus({preventScroll:true});};
      g.addEventListener('click', activate); g.addEventListener('keydown', e => {if (['Enter',' '].includes(e.key)) {e.preventDefault();activate();}}); peopleLayer.append(g);
    });
    scene = nodes;
    drawInterests(nodes);
  }
  const finiteAmount = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const interests = new Map();
  const interestRequests = new Set();
  function interestRows(personId) {
    const rows = new Map();
    const sorted = (interests.get(personId)?.events || [])
      .filter(e => e.ticker !== root.dataset.ticker && (Date.parse(e.filed_at) || 0) <= cutoff)
      .sort((a,b) => Date.parse(b.filed_at) - Date.parse(a.filed_at) || (Date.parse(b.occurred_at) || 0) - (Date.parse(a.occurred_at) || 0) || b.id.localeCompare(a.id, 'en', {numeric:true}));
    sorted.forEach(event => {
      const person = event.people.find(p => p.id === personId);
      if (!person) return;
      const relationship = event.view === 'ownership' ? 'Stake' : ({Bought:'Buy', Sold:'Sell'}[event.action] || event.action);
      const add = (kind, value, unit) => {
        const key = JSON.stringify([event.ticker, kind, kind === 'Director' ? '' : event.security || '', kind === 'Director' ? '' : event.security_type || '', kind === 'Director' ? '' : event.ownership || '']);
        if (!rows.has(key)) rows.set(key, {key, event, relationship:kind, value:finiteAmount(value) ? value : null, unit});
      };
      add(relationship, event.view === 'ownership' ? event.percent : event.value, event.view === 'ownership' ? '%' : '$');
      if (/\bdirector\b/i.test(person.role)) add('Director', null, 'role');
    });
    return [...rows.values()];
  }
  function drawInterests(nodes) {
    const active = document.activeElement;
    const focusedKey = active?.getAttribute('data-interest');
    interestsLayer.replaceChildren();
    nodes.forEach(node => {
      const rows = interestRows(node.id);
      const maxima = {'%':0,'$':0};
      rows.forEach(row => {if (row.value != null && row.unit in maxima) maxima[row.unit] = Math.max(maxima[row.unit],row.value);});
      rows.forEach((row,i) => {
        // Dots follow the upper arc so the person's name stays readable.
        const orbit = node.radius + 13;
        const angle = rows.length === 1 ? -Math.PI/2 : 5*Math.PI/6 + i*4*Math.PI/3/(rows.length-1);
        const x = node.x + orbit*Math.cos(angle), y = node.y + orbit*Math.sin(angle);
        const radius = row.value == null ? 4 : Math.sqrt(9 + 27*(maxima[row.unit] ? row.value/maxima[row.unit] : 0));
        const tone = row.relationship === 'Sell' ? 'sell' : ['Buy','Stake'].includes(row.relationship) ? 'buy' : 'role';
        const amount = row.value == null ? '' : row.unit === '%' ? `${number(row.value)}%` : money(row.value);
        const label = `${row.event.ticker} · ${row.relationship}${amount ? ' · '+amount : ''} · Filed ${date(row.event.filed_at)}`;
        const link = svg('a', {href:`/t/${encodeURIComponent(row.event.ticker)}`,class:`map-interest ${tone}`,tabindex:0,'aria-label':label,'data-interest':node.id+':'+row.key});
        link.append(svg('circle',{cx:x,cy:y,r:Math.max(7,radius),class:'map-interest-hit'}),svg('circle',{cx:x,cy:y,r:radius,class:'map-interest-dot','data-amount':row.value ?? 'unknown'}),svg('title',{},label));
        interestsLayer.append(link);
      });
    });
    if (focusedKey) interestsLayer.querySelector(`[data-interest="${CSS.escape(focusedKey)}"]`)?.focus({preventScroll:true});
  }
  async function loadInterests(person) {
    if (!person.id.startsWith('sec:') || interests.has(person.id)) return;
    const state = {events:[]}; interests.set(person.id,state);
    const controller = new AbortController(); interestRequests.add(controller);
    let cursor = null;
    try {
      do {
        const query = new URLSearchParams({person_id:person.id}); if (cursor) query.set('cursor',cursor);
        const response = await fetch(`/api/stocks/${encodeURIComponent(root.dataset.ticker)}/map/connections?${query}`,{signal:controller.signal,headers:{Accept:'application/json'}});
        if (!response.ok) throw new Error('connections');
        const data = await response.json();
        if (data.person_id !== person.id || !Array.isArray(data.events)) throw new Error('person');
        const unique = new Map(state.events.map(e=>[e.id,e])); data.events.forEach(e=>unique.set(e.id,e)); state.events=[...unique.values()];
        cursor = data.next_cursor;
        drawInterests(scene);
      } while (cursor && !controller.signal.aborted);
    } catch (_) { interests.delete(person.id); }
    finally {interestRequests.delete(controller);}
  }
  function loadVisibleInterests() {
    const shown = peopleFor(subset()).slice(page*pageSize(),(page+1)*pageSize());
    shown.forEach(loadInterests);
  }
  window.addEventListener('pagehide',()=>interestRequests.forEach(controller=>controller.abort()));

  function blendScenes(first, last, t) {
    const before = new Map(first.map(node => [node.id, node])), after = new Map(last.map(node => [node.id, node]));
    const {cx, cy} = metrics();
    const keys = [...after.keys(), ...[...before.keys()].filter(key => !after.has(key))];
    return keys.map(key => {
      const start = before.get(key), end = after.get(key);
      if (start && end) return {...end, x:start.x + (end.x - start.x) * t, y:start.y + (end.y - start.y) * t, opacity:1};
      if (end) return {...end, x:cx, y:cy, opacity:t};
      return {...start, opacity:1 - t};
    });
  }
  function renderPeople(event, animate, restoreFocus, focusSelector) {
    const people = peopleFor(subset()), size = pageSize();
    page = Math.min(page, Math.max(0, Math.ceil(people.length / size) - 1));
    const target = buildScene(event);
    $('paging').hidden = people.length <= size;
    $('page').textContent = people.length ? `${page*size+1}–${page*size+target.length} of ${people.length} people` : '';
    $('previous').disabled = page === 0; $('next').disabled = (page+1)*size >= people.length;
    const duration = animate && !motion.matches && scene.length ? ANIMATION_MS : 0;
    cancelAnimationFrame(animation);
    const run = ++generation, first = scene, start = performance.now();
    graph.dataset.phase = duration ? 'moving' : 'settled';
    function tick(now) {
      if (run !== generation) return;
      const t = duration ? Math.min(1, (now - start) / duration) : 1;
      drawScene(t === 1 ? target : blendScenes(first, target, t * t * (3 - 2 * t)));
      if (t < 1) { animation = requestAnimationFrame(tick); return; }
      graph.dataset.phase = 'settled';
      if (restoreFocus && focusSelector) (root.querySelector(focusSelector) || ringLayer.querySelector('[data-map-score-center]'))?.focus({preventScroll:true});
    }
    tick(start);
  }
  function renderFilings() {
    const list = $('events'); list.replaceChildren();
    const pageCount = Math.max(1, Math.ceil(events.length / FILINGS_PAGE_SIZE));
    filingsPageNumber = Math.min(filingsPageNumber, pageCount - 1);
    const slice = events.slice(filingsPageNumber * FILINGS_PAGE_SIZE, filingsPageNumber * FILINGS_PAGE_SIZE + FILINGS_PAGE_SIZE);
    slice.forEach(e => {
      const button = make('button',null,'map-event'); button.type = 'button'; button.dataset.eventId = e.id; button.setAttribute('aria-pressed',String(e.id === selected));
      const main = make('span'); main.append(make('strong',`${names(e)} · ${e.action}`),make('small',`${e.basis} · Filed ${date(e.filed_at)}${e.amendment ? ' · Amendment' : ''}`));
      button.append(make('span','●',e.tone),main,make('span',amount(e),'event-money'));
      button.addEventListener('click',() => scrub(e)); list.append(button);
    });
    if (!events.length) list.append(make('p','Saved filings will appear here.','map-note'));
    $('filings-paging').hidden = events.length <= FILINGS_PAGE_SIZE;
    $('filings-page').textContent = events.length ? `${filingsPageNumber * FILINGS_PAGE_SIZE + 1}–${filingsPageNumber * FILINGS_PAGE_SIZE + slice.length} of ${events.length}` : '';
    $('filings-previous').disabled = filingsPageNumber === 0;
    $('filings-next').disabled = (filingsPageNumber + 1) * FILINGS_PAGE_SIZE >= events.length;
  }
  function choose(event, personId) {
    selected = event?.id || null; pinned = null; hovered = null; focused = null;
    if (event) {
      const at = events.findIndex(row => row.id === event.id);
      if (at >= 0) filingsPageNumber = Math.floor(at / FILINGS_PAGE_SIZE);
    }
    const people = peopleFor(subset()), at = people.findIndex(p => p.id === (personId || event?.people[0]?.id));
    if (at >= 0) page = Math.floor(at / pageSize());
    ringDirty = true;
    render(false, true);
  }
  function scrub(event) {
    const time = Date.parse(event.filed_at);
    selected = event.id;
    if (Number.isFinite(time) && time !== cutoff) {
      cutoff = time;
      const people = peopleFor(subset()), at = people.findIndex(p => p.events.some(row => row.id === event.id));
      page = at >= 0 ? Math.floor(at / pageSize()) : 0;
    }
    const at = events.findIndex(row => row.id === event.id);
    if (at >= 0) filingsPageNumber = Math.floor(at / FILINGS_PAGE_SIZE);
    render(false, true);
    $('events').querySelector(`[data-event-id="${CSS.escape(event.id)}"]`)?.focus({preventScroll:true});
  }
  function render(restoreFocus = true, animate = true) {
    graph.setAttribute('viewBox', small.matches ? '0 0 360 350' : '0 0 760 440');
    const active = document.activeElement;
    const focusSelector = active?.hasAttribute('data-score-key') ? `[data-score-key="${CSS.escape(active.dataset.scoreKey)}"]` : active?.hasAttribute('data-person') ? `[data-person="${CSS.escape(active.dataset.person)}"]` : active?.hasAttribute('data-map-score-center') ? '[data-map-score-center]' : active?.hasAttribute('data-event-id') ? `[data-event-id="${CSS.escape(active.dataset.eventId)}"]` : null;
    if (ringDirty) { drawRing(); ringDirty = false; }
    const event = subset().find(row => row.id === selected) || null;
    selected = event?.id || null;
    renderPeople(event, animate, restoreFocus, focusSelector);
    renderFilings();
    source(event);
    loadVisibleInterests();
  }
  async function load() {
    if (pending) return; pending = true; $('load').disabled = true;
    $('status').textContent = events.length ? 'Loading older filings…' : 'Loading saved SEC filings…';
    try {
      const res = await fetch(`/api/stocks/${encodeURIComponent(root.dataset.ticker)}/map${cursor ? '?cursor='+encodeURIComponent(cursor) : ''}`,{headers:{Accept:'application/json'}});
      if (!res.ok) throw new Error('load');
      const data = await res.json(); if (data.ticker !== root.dataset.ticker) throw new Error('subject');
      const unique = new Map(events.map(e => [e.id,e])); data.events.forEach(e => unique.set(e.id,e));
      events = [...unique.values()].sort((a,b) => Date.parse(b.filed_at)-Date.parse(a.filed_at) || b.id.localeCompare(a.id));
      cursor = data.next_cursor;
      $('load').hidden = !cursor; $('load').textContent = 'Load older filings';
      $('status').textContent = '';
      render(false, true);
    } catch (_) { $('status').textContent = 'Please retry to load the saved filings.'; $('load').hidden = false; $('load').textContent = 'Retry loading filings'; }
    finally {pending = false; $('load').disabled = false;}
  }
  $('filings-previous').addEventListener('click',() => {filingsPageNumber--;renderFilings();}); $('filings-next').addEventListener('click',() => {filingsPageNumber++;renderFilings();});
  $('previous').addEventListener('click',() => {page--;render(true,true);}); $('next').addEventListener('click',() => {page++;render(true,true);});
  $('load').addEventListener('click',load);
  $('score-return').addEventListener('click', overview);
  root.addEventListener('keydown', event => {if (event.key === 'Escape' && (pinned || selected)) {event.preventDefault(); overview();}});
  small.addEventListener('change',() => {page = 0; ringDirty = true; render(false,false);});
  motion.addEventListener('change', () => render(false,false));
  screenNode?.addEventListener('rati:screen-detail', event => {
    const next = event.detail;
    if (next?.market !== 'stocks' || next.item?.id !== root.dataset.ticker) return;
    if (scoreData(item) === scoreData(next.item)) return;
    item = next.item;
    hovered = null; focused = null;
    readScore();
    if (!contributions.some(part => part.key === pinned)) pinned = null;
    const active = document.activeElement;
    const focusSelector = active?.hasAttribute('data-score-key') ? `[data-score-key="${CSS.escape(active.dataset.scoreKey)}"]` : active?.hasAttribute('data-map-score-center') ? '[data-map-score-center]' : null;
    drawRing(); ringDirty = false;
    const selectedEvent = subset().find(row => row.id === selected) || null;
    selected = selectedEvent?.id || null;
    source(selectedEvent);
    if (focusSelector) (root.querySelector(focusSelector) || ringLayer.querySelector('[data-map-score-center]'))?.focus({preventScroll:true});
  });
  render();
  load();
})();
