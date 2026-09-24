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
  const names = e => e.people.map(p => p.name).join(' + ') || 'Reporting wallet';
  const amount = e => e.view === 'ownership' ? (e.percent == null ? 'See filing' : number(e.percent) + '% of class') : money(e.value);
  const screenNode = document.getElementById('screenData');
  const initial = screenNode?.ratiScreenDetail || JSON.parse(screenNode?.textContent || '{}');
  let item = initial.market === 'stocks' && initial.item?.id === root.dataset.ticker ? initial.item : {};
  let contributions = [], controls = [], total = 0, score = '—', glyph = {};
  const scoreData = value => {
    const g = value.indicator || {};
    return JSON.stringify([g.score,g.band,g.slices,g.mix_state,g.sentiment,g.sentiment_mix,g.risk,g.risk_factors,value.score_trace ?? null]);
  };
  const allowed = (value, options, fallback) => options.includes(value) ? value : fallback;
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  function readScore() {
    const value = item.indicator || {};
    glyph = {
      band: allowed(value.band, [1,2,3], 1),
      sentimentMix:window.RatiRingGlyph.readSentiment(value.sentiment_mix),
      sentiment: allowed(value.sentiment, ['positive','negative','neutral','unknown'], 'unknown'),
      risk: allowed(value.risk, ['none','detected','significant','unknown'], 'unknown'),
      mix: allowed(value.mix_state, ['available','zero','unknown'], 'unknown'),
    };
    const parts = Array.isArray(value.slices) ? value.slices : [];
    contributions = ['market','evidence','social','cluster'].map((key, index) => {
      const part = parts.find(part => part?.key === key);
      return {key, label:['Market','Filings + news','External social','Cluster holdings'][index], value:finite(part?.value) ? Math.max(0,part.value) : 0};
    }).filter(part => part.value > 0);
    total = contributions.reduce((sum, part) => sum + part.value, 0);
    if (glyph.mix !== 'available' || !Number.isFinite(total) || total <= 0) { contributions = []; total = 0; }
    contributions.forEach(part => { part.share = part.value / total; });
    score = finite(value.score) ? number(value.score) : '—';
    controls = [...contributions, {key:'sentiment',label:'Filing sentiment'}];
    if (glyph.risk !== 'none') controls.push({key:'risk',label:'Risk factors'});
  }
  readScore();
  const points = value => `${value > 0 ? '+' : ''}${number(value)} pts`;
  const percent = part => `${number(Math.round(part.share * 1000) / 10)}% of attention contributions`;
  const color = part => `var(--indicator-${part.key})`;
  const metrics = () => window.RatiRingGlyph.metrics(glyph, small.matches);
  const graph = $('graph');
  const peopleLayer = svg('g', {class:'map-people'});
  const ringLayer = svg('g', {class:'map-ring map-glyph'});
  const interestsLayer = svg('g', {class:'map-interests'});
  graph.append(peopleLayer, interestsLayer, ringLayer);
  window.ratiOrbit?.attach(graph);
  let pinned = null, hovered = null, focused = null;
  let events = [], cursor = null, selected = null;
  let cutoff = Infinity, page = 0, pending = false, filingsPageNumber = 0;
  let ringDirty = true, scene = [], animation = 0, generation = 0;
  const small = window.matchMedia('(max-width:500px)');
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const pageSize = () => small.matches ? 4 : 12;
  const FILINGS_PAGE_SIZE = 5;
  const ANIMATION_MS = 320;
  const subset = () => events.filter(e => (Date.parse(e.filed_at) || 0) <= cutoff);
  function scorePanel(part) {
    const panel = $('selection'); panel.replaceChildren();
    // A compact reading inside the existing map, not another ticker-page card.
    const reading = `Attention ${score === '—' ? 'unavailable' : score + ' points'} · Filing sentiment: ${glyph.sentimentMix.reading} · ${window.RatiRingGlyph.riskReading(glyph.risk)}`;
    panel.append(make('p', reading, 'map-glyph-reading'));
    if (part) {
      panel.append(make('h4', part.label));
      if (part.key === 'risk') panel.append(make('p', `${window.RatiRingGlyph.riskReading(glyph.risk)}.`, 'map-risk-reading'));
      if (part.key === 'risk' && Array.isArray(item.indicator?.risk_factors) && item.indicator.risk_factors.length) {
        const factors = make('ul',null,'map-risk-factors');
        item.indicator.risk_factors.forEach(reason => factors.append(make('li',reason))); panel.append(factors);
      }
      if (part.key === 'sentiment') panel.append(make('p', `${glyph.sentimentMix.reading}. ${glyph.sentimentMix.basis}.`, 'map-sentiment-reading'));
      else if (part.key !== 'risk') panel.append(make('p', `${points(part.value)} · ${percent(part)}`, 'map-score-breakdown'));
      const keys = part.key === 'evidence' ? ['sec_event','news'] : part.key === 'social' ? ['social_search'] : part.key === 'risk' ? ['rug'] : [part.key];
      for (const key of keys) {
        const rows = item.score_trace?.[key];
        if (!Array.isArray(rows) || !rows.length) continue;
        const trace = make('dl', null, 'map-score-trace');
        const label = key === 'sec_event' ? 'SEC filing' : key === 'news' ? 'News' : part.label;
        trace.setAttribute('aria-label', `${label} component trace`);
        rows.filter(row => row && typeof row === 'object').forEach(({label, value}) => {
          const row = make('div'); row.append(make('dt', label), make('dd', value)); trace.append(row);
        });
        panel.append(trace);
      }
    }
    const badges = make('div', null, 'map-score-badges');
    const legend = make('ul', null, 'map-score-legend');
    contributions.forEach(driver => {
      const row = make('li');
      const swatch = make('span', null, 'map-score-swatch'); swatch.style.background = color(driver); swatch.dataset.pattern = driver.key; swatch.setAttribute('aria-hidden', 'true');
      row.append(swatch, make('span', driver.label), make('strong', points(driver.value))); legend.append(row);
    });
    if (legend.children.length) { badges.append(legend); panel.append(badges); }
    else panel.append(make('p', glyph.mix === 'zero' ? 'No attention contributions.' : 'Attention breakdown unavailable.', 'map-glyph-empty'));
  }
  function context() {
    source(subset().find(event => event.id === selected));
  }
  function overview() {
    selected = null; pinned = null; hovered = null; focused = null; ringDirty = true;
    render(false, false);
    graph.querySelector('[data-map-score-center]')?.focus({preventScroll:true});
  }
  function wireControl(element, part) {
    element.setAttribute('role','button'); element.setAttribute('tabindex','0');
    element.dataset.scoreKey = part.key;
    element.setAttribute('aria-pressed',String(pinned === part.key));
    element.addEventListener('mouseenter', () => {hovered = part; context();});
    element.addEventListener('mouseleave', () => {hovered = null; context();});
    element.addEventListener('focus', () => {focused = part; context();});
    element.addEventListener('blur', () => {focused = null; context();});
    const activate = () => {
      pinned = part.key; selected = null; hovered = null; focused = null; ringDirty = true;
      render(false, false);
      ringLayer.querySelector(`[data-score-key="${CSS.escape(part.key)}"]`)?.focus({preventScroll:true});
    };
    element.addEventListener('click', activate);
    element.addEventListener('keydown', event => {
      if (['Enter',' '].includes(event.key)) {event.preventDefault(); activate();}
      if (['ArrowRight','ArrowDown','ArrowLeft','ArrowUp','Home','End'].includes(event.key)) {
        event.preventDefault();
        const targets = [...ringLayer.querySelectorAll('[data-score-key]')], index = targets.indexOf(element);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? targets.length - 1 : (index + (['ArrowRight','ArrowDown'].includes(event.key) ? 1 : -1) + targets.length) % targets.length;
        targets[next].focus({preventScroll:true});
      }
    });
  }
  function drawRing() {
    window.RatiRingGlyph.draw({
      ringLayer, graph, glyph, small:small.matches, contributions, controls, score,
      name:root.dataset.ticker, toneLabel:'Filing sentiment',
      centerAttributes:{'data-map-score-center':''}, points, percent, wireControl, overview,
    });
  }
  function source(event) {
    const preview = hovered || focused;
    root.querySelector('.map-workspace').classList.toggle('map-overview', !event);
    const panel = $('selection');
    $('score-return').hidden = !event && !pinned;
    if (preview || !event) {
      scorePanel(preview || controls.find(part => part.key === pinned));
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
    if (event.joint) panel.append(make('p','Joint report. The source describes how the reporting wallets share this interest.'));
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
      const angle = -Math.PI / 2 + i * Math.PI * 2 / shown.length;
      const x = small.matches ? (left ? 85 : 275) : 380 + 270 * Math.cos(angle);
      const y = small.matches ? 64 + Math.floor(i/2)*210 : 218 + 150 * Math.sin(angle);
      const first = person.events[0];
      const tones = new Set(person.events.map(e => e.tone));
      return {id:person.id, wallet_id:person.wallet_id, name:person.name, first, events:person.events, eventCount:person.events.length, radius:finiteAmount(magnitude(first)) ? Math.sqrt(144 + 640 * (maxAmount(first.view) ? magnitude(first)/maxAmount(first.view) : 0)) : 16, tone:tones.size === 1 ? first.tone : 'neutral', active:!!event?.people.some(p => p.id === person.id), x, y};
    });
  }
  function drawScene(nodes) {
    peopleLayer.replaceChildren();
    const {cx, cy} = metrics();
    graph.dataset.orbitCenter = `${cx},${cy}`;
    // Desktop wallets sit on this ellipse; nodes travel along it so the ring
    // stays put instead of swinging around with them. Mobile stacks columns,
    // so it stays still.
    if (small.matches) delete graph.dataset.orbitTrack;
    else graph.dataset.orbitTrack = '270,150';
    const orbiting = !small.matches;
    nodes.forEach(node => {
      const opacity = node.opacity ?? 1;
      node.events.forEach((event, index) => {
        const bend = (index - (node.events.length-1)/2)*Math.min(8,48/Math.max(1,node.events.length-1));
        const dx = node.x-cx, dy = node.y-cy, length = Math.hypot(dx,dy) || 1;
        const line = svg('path', {d:`M ${cx} ${cy} Q ${(cx+node.x)/2-dy/length*bend} ${(cy+node.y)/2+dx/length*bend} ${node.x} ${node.y}`, class:`map-edge ${event.tone}`, fill:'none', 'stroke-opacity':opacity, 'data-edge-event':event.id, 'vector-effect':'non-scaling-stroke', ...(orbiting ? {'data-orbit':''} : {})});
        line.append(svg('title', {}, `${event.action} · ${amount(event)} · Filed ${date(event.filed_at)}`));
        line.addEventListener('click', () => choose(event, node.id)); peopleLayer.append(line);
      });
      const href = node.wallet_id ? `/wallet/${encodeURIComponent(node.wallet_id)}` : `/wallets/stocks/${encodeURIComponent(root.dataset.ticker)}/${encodeURIComponent(node.id)}`;
      const dense = nodes.length > 8 ? ' dense' : '';
      const g = svg('a', {href, class:`map-person ${node.tone}${dense}`, tabindex:0, 'aria-label':`${node.name}: stocks and events`, 'aria-pressed':String(!!node.active), 'data-person':node.id, ...(orbiting ? {'data-orbit-anchor':`${node.x},${node.y}`} : {}), opacity});
      g.append(svg('circle', {cx:node.x, cy:node.y, r:node.radius}));
      const initials = node.name.split(/\s+/).slice(0,2).map(n => n[0]).join('');
      g.append(svg('text', {x:node.x, y:node.y+5, 'text-anchor':'middle'}, initials));
      const maxName = small.matches ? 21 : nodes.length > 8 ? 22 : 28;
      g.append(svg('text', {x:node.x, y:node.y+42, 'text-anchor':'middle'}, node.name.length > maxName ? node.name.slice(0,maxName-2)+'…' : node.name));
      const action = small.matches ? ({'Exercise or conversion':'Exercise / conversion','Tax or exercise payment':'Tax / exercise payment'}[node.first.action] || node.first.action) : `${node.first.action} · ${amount(node.first)}`;
      g.append(svg('text', {x:node.x, y:node.y+59, 'text-anchor':'middle', class:'map-node-action'}, action));
      g.append(svg('title', {}, node.name));
      const transition = () => {
        graph.querySelectorAll('[data-person]').forEach(node => node.style.viewTransitionName = 'none');
        g.style.viewTransitionName = 'entity-focus';
      };
      g.addEventListener('pointerdown', transition);
      g.addEventListener('keydown', event => {if (event.key === 'Enter') transition();});
      peopleLayer.append(g);
    });
    scene = nodes;
    drawInterests(nodes);
  }
  const finiteAmount = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const interests = new Map();
  const interestRequests = new Set();
  function interestRows(personId) {
    const groups = new Map();
    (interests.get(personId)?.events || [])
      .filter(e => e.ticker !== root.dataset.ticker && (Date.parse(e.filed_at) || 0) <= cutoff)
      .forEach(event => {
        if (!event.people.some(person => person.id === personId)) return;
        if (!groups.has(event.ticker)) groups.set(event.ticker, new Map());
        groups.get(event.ticker).set(event.id, event);
      });
    return [...groups].map(([ticker, events]) => ({ticker, events:[...events.values()]}));
  }
  function drawInterests(nodes) {
    const focusedKey = document.activeElement?.getAttribute('data-interest');
    interestsLayer.replaceChildren();
    nodes.forEach(node => {
      const stocks = interestRows(node.id);
      const magnitude = event => event.view === 'ownership' ? event.percent : event.value;
      const unit = event => event.view === 'ownership' ? '%' : '$';
      const tone = event => event.action === 'Sold' ? 'sell' : event.view === 'ownership' || event.action === 'Bought' ? 'buy' : 'role';
      const maxima = {'%':0,'$':0};
      stocks.flatMap(stock => stock.events).forEach(event => {
        if (finiteAmount(magnitude(event))) maxima[unit(event)] = Math.max(maxima[unit(event)], magnitude(event));
      });
      stocks.forEach((stock,i) => {
        const orbit = node.radius + 17;
        const angle = stocks.length === 1 ? -Math.PI/2 : 5*Math.PI/6 + i*4*Math.PI/3/(stocks.length-1);
        const x = node.x + orbit*Math.cos(angle), y = node.y + orbit*Math.sin(angle);
        const weights = stock.events.filter(e => finiteAmount(magnitude(e))).map(e => maxima[unit(e)] ? magnitude(e)/maxima[unit(e)] : 0);
        const radius = weights.length ? Math.sqrt(9 + 27*Math.max(...weights)) : 4;
        const tones = new Set(stock.events.map(tone));
        const label = `${stock.ticker} · ${stock.events.length} events`;
        const link = svg('a', {href:`/stock/${encodeURIComponent(stock.ticker)}`,class:`map-interest ${tones.size === 1 ? [...tones][0] : 'role'}`,tabindex:0,'aria-label':label,'data-interest':node.id+':'+stock.ticker, ...(small.matches ? {} : {'data-orbit-anchor':`${node.x},${node.y}`})});
        stock.events.forEach((event,index) => {
          const bend = (index-(stock.events.length-1)/2)*Math.min(3,12/Math.max(1,stock.events.length-1));
          const sx = node.x + node.radius*Math.cos(angle), sy = node.y + node.radius*Math.sin(angle);
          const line = svg('path', {d:`M ${sx} ${sy} Q ${(sx+x)/2-Math.sin(angle)*bend} ${(sy+y)/2+Math.cos(angle)*bend} ${x} ${y}`,class:`map-interest-edge ${tone(event)}`,'data-interest-event':event.id});
          line.append(svg('title', {}, `${stock.ticker} · ${event.action} · ${amount(event)} · Filed ${date(event.filed_at)}`)); link.append(line);
        });
        link.append(svg('circle',{cx:x,cy:y,r:Math.max(7,radius),class:'map-interest-hit'}),svg('circle',{cx:x,cy:y,r:radius,class:'map-interest-dot'}),svg('title',{},label));
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
    $('page').textContent = people.length ? `${page*size+1}–${page*size+target.length} of ${people.length} wallets` : '';
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
      const button = make('button',null,'wallet-event map-filing'); button.type = 'button'; button.dataset.eventId = e.id; button.setAttribute('aria-pressed',String(e.id === selected));
      button.dataset.tone = e.action === 'Sold' ? 'sell' : e.action === 'Bought' || e.view === 'ownership' ? 'buy' : 'other';
      const description = `${names(e)} · ${e.action} · ${e.security || 'See filing'}${e.shares == null ? '' : ` · ${number(e.shares)} shares`} · ${amount(e)} · ${e.basis} · ${e.form} · Filed ${date(e.filed_at)}${e.amendment ? ' · Amendment' : ''}`;
      button.title = description; button.setAttribute('aria-label',description);
      const subject = make('span',null,'wallet-event-ticker'); subject.append(make('strong',names(e)));
      const detail = make('span',`${e.security || ''}${e.shares == null ? '' : ` · ${number(e.shares)} shares`}`,'wallet-event-detail');
      const filed = make('time',e.filed_at?.slice(5,10),'wallet-filing'); filed.dateTime = e.filed_at?.slice(0,10) || '';
      button.append(subject,make('span',e.action,'wallet-event-action'),detail,make('span',amount(e),'wallet-event-value'),filed);
      button.addEventListener('click',() => scrub(e)); list.append(button);
    });
    $('filings').hidden = !events.length;
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
    if (!controls.some(part => part.key === pinned)) pinned = null;
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
