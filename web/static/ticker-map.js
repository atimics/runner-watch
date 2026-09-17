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
  const initial = JSON.parse(screenNode?.textContent || '{}');
  let item = initial.market === 'stocks' && initial.item?.id === root.dataset.ticker ? initial.item : {};
  let drivers = [], positive = [], penalties = [], total = 0, score = '—';
  const scoreData = value => JSON.stringify([value.score ?? null, value.score_detail ?? null]);
  function readScore() {
    const parts = value => Array.isArray(value) ? value.filter(part => part && Number.isFinite(part.value)) : [];
    drivers = parts(item.score_detail?.drivers);
    positive = drivers.filter(part => part.value > 0);
    penalties = [...parts(item.score_detail?.penalties), ...drivers.filter(part => part.value < 0)].filter(part => part.value !== 0);
    total = positive.reduce((sum, part) => sum + part.value, 0);
    score = Number.isFinite(item.score) ? number(Math.round(item.score)) : '—';
  }
  function scoreTime(value) {
    const note = $('score-time');
    note.replaceChildren('Current score · ');
    if (Number.isFinite(item.score) && value && Number.isFinite(Date.parse(value))) {
      const time = make('time', value); time.dateTime = value; note.append('As of ', time);
    } else note.append('Timestamp unavailable');
    note.append('. Filing dates filter evidence, not the score.');
  }
  readScore();
  const points = value => `${value > 0 ? '+' : ''}${number(value)} pts`;
  const percent = part => `${number(Math.round(part.value / total * 1000) / 10)}% of positive contributions`;
  const color = part => `var(--score-${part.key}, var(--muted))`;
  let pinned = null, hovered = null, focused = null;
  let events = [], dates = [], cursor = null, loaded = 0, coverage = {}, selected = null;
  let view = 'all', filter = 'all', cutoff = Infinity, page = 0, pending = false;
  const small = window.matchMedia('(max-width:500px)');
  const pageSize = () => small.matches ? 4 : 8;
  const subset = () => events.filter(e => (Date.parse(e.filed_at) || 0) <= cutoff &&
    (filter === 'all' || (filter === 'other' ? !['Bought','Sold'].includes(e.action) : e.action === filter)));
  function scorePanel(part) {
    const panel = $('selection'); panel.replaceChildren();
    panel.append(make('h3', `Current score ${score}`));
    if (part) {
      panel.append(make('h4', part.label), make('p', `${points(part.value)} · ${percent(part)}`, 'map-score-breakdown'));
      panel.append(make('p', pinned === part.key ? 'Pinned contribution. Return to score overview to clear.' : 'Click or press Enter to pin this contribution.', 'map-note'));
    }
    const legend = make('ul', null, 'map-score-legend');
    drivers.filter(driver => driver.value >= 0).forEach(driver => {
      const row = make('li');
      const swatch = make('span', null, 'map-score-swatch'); swatch.style.background = color(driver); swatch.setAttribute('aria-hidden', 'true');
      row.append(swatch, make('span', driver.label), make('strong', points(driver.value))); legend.append(row);
    });
    if (legend.children.length) panel.append(legend);
    if (!positive.length) panel.append(make('p', item.score_detail ? 'No positive contributions.' : 'Score breakdown unavailable.', 'map-note'));
    panel.append(make('p', 'Ring shares use positive contributions only, not the net score.', 'map-note'));
    panel.append(make('h4', 'Penalties'));
    if (penalties.length) {
      const list = make('ul', null, 'map-score-penalties');
      penalties.forEach(penalty => {const row = make('li'); row.append(make('span', penalty.label), make('strong', points(penalty.value))); list.append(row);});
      panel.append(list);
    } else panel.append(make('p', item.score_detail ? 'No penalties applied.' : 'Penalty data unavailable.', 'map-note'));
  }
  function context() {
    source(subset().find(event => event.id === selected));
  }
  function overview() {
    selected = null; pinned = null; hovered = null; focused = null; render(false);
    $('graph').querySelector('[data-map-score-center]')?.focus({preventScroll:true});
  }
  function ring(graph, cx, cy) {
    const radius = small.matches ? 48 : 62, width = small.matches ? 16 : 20;
    graph.append(svg('circle', {cx, cy, r:radius, class:'map-score-track', 'stroke-width':width}));
    let angle = -Math.PI / 2;
    positive.forEach(part => {
      const sweep = part.value / total * Math.PI * 2, end = angle + sweep;
      const attrs = {class:'map-score-segment', role:'button', tabindex:0, 'data-score-key':part.key, 'aria-label':`${part.label}: ${points(part.value)}, ${percent(part)}`, 'aria-pressed':String(pinned === part.key), 'stroke-width':width};
      const segment = positive.length === 1 ? svg('circle', {...attrs, cx, cy, r:radius}) : svg('path', {...attrs, d:`M ${cx + radius * Math.cos(angle)} ${cy + radius * Math.sin(angle)} A ${radius} ${radius} 0 ${sweep > Math.PI ? 1 : 0} 1 ${cx + radius * Math.cos(end)} ${cy + radius * Math.sin(end)}`});
      segment.style.stroke = color(part); angle = end;
      segment.append(svg('title', {}, `${part.label}: ${points(part.value)} · ${percent(part)}`));
      segment.addEventListener('mouseenter', () => {hovered = part; context();});
      segment.addEventListener('mouseleave', () => {hovered = null; context();});
      segment.addEventListener('focus', () => {focused = part; context();});
      segment.addEventListener('blur', () => {focused = null; context();});
      const activate = () => {
        pinned = part.key; selected = null; hovered = null; focused = null; render();
        graph.querySelector(`[data-score-key="${CSS.escape(part.key)}"]`)?.focus({preventScroll:true});
      };
      segment.addEventListener('click', activate);
      segment.addEventListener('keydown', event => {
        if (['Enter', ' '].includes(event.key)) {event.preventDefault(); activate();}
        if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
          event.preventDefault();
          const segments = [...graph.querySelectorAll('.map-score-segment')], index = segments.indexOf(segment);
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? segments.length - 1 : (index + (['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1) + segments.length) % segments.length;
          segments[next].focus({preventScroll:true});
        }
      });
      graph.append(segment);
    });
    const center = svg('g', {role:'button', tabindex:0, class:'map-score-center', 'data-map-score-center':'', 'aria-label':`${root.dataset.ticker}, current score ${score}. Show score overview.`});
    center.append(svg('circle', {cx, cy, r:radius - width / 2 - 3, class:'map-center'}), svg('text', {x:cx, y:cy - 6, 'text-anchor':'middle', class:'map-center-text'}, root.dataset.ticker), svg('text', {x:cx, y:cy + 16, 'text-anchor':'middle', class:'map-center-score'}, score));
    center.addEventListener('click', overview);
    center.addEventListener('keydown', event => {if (['Enter', ' '].includes(event.key)) {event.preventDefault(); overview();}});
    graph.append(center);
  }
  function source(event) {
    const preview = hovered || focused;
    $('context').textContent = preview || !event ? 'SCORE' : 'FILING';
    $('score-return').hidden = !event && !pinned;
    if (preview || !event) {
      scorePanel(preview || positive.find(part => part.key === pinned));
      document.dispatchEvent(new CustomEvent('rati:map-time', {detail:{time:null}})); return;
    }
    const panel = $('selection'); panel.replaceChildren();
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
      const notes = make('details'); notes.append(make('summary','Filing notes'),make('p',[event.footnotes,event.ownership_detail].filter(Boolean).join(' '))); panel.append(notes);
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
  function choose(event, personId) {
    selected = event?.id || null; pinned = null; hovered = null; focused = null;
    const people = peopleFor(subset()), at = people.findIndex(p => p.id === (personId || event?.people[0]?.id));
    if (at >= 0) page = Math.floor(at / pageSize());
    render(false);
  }
  function render(restoreFocus = true) {
    const rows = subset(), people = peopleFor(rows), graph = $('graph');
    const active = document.activeElement;
    const focusSelector = active?.hasAttribute('data-score-key') ? `[data-score-key="${CSS.escape(active.dataset.scoreKey)}"]` : active?.hasAttribute('data-person') ? `[data-person="${CSS.escape(active.dataset.person)}"]` : active?.hasAttribute('data-map-score-center') ? '[data-map-score-center]' : active?.hasAttribute('data-event-id') ? `[data-event-id="${CSS.escape(active.dataset.eventId)}"]` : null;
    hovered = null; focused = null;
    const event = rows.find(e => e.id === selected); selected = event?.id || null;
    const size = pageSize(), cx = small.matches ? 180 : 380, cy = small.matches ? 184 : 218;
    page = Math.min(page, Math.max(0, Math.ceil(people.length / size)-1));
    graph.setAttribute('viewBox', small.matches ? '0 0 360 350' : '0 0 760 440');
    graph.replaceChildren();
    const shown = people.slice(page*size,page*size+size);
    shown.forEach((person,i) => {
      const left = i % 2 === 0, x = small.matches ? (left ? 85 : 275) : (left ? 180 : 580);
      const y = small.matches ? 64 + Math.floor(i/2)*210 : 62 + Math.floor(i/2)*103;
      const first = person.events[0], active = event?.people.some(p => p.id === person.id);
      const tones = new Set(person.events.map(e => e.tone));
      const tone = tones.size === 1 ? first.tone : 'neutral';
      const line = svg('line',{x1:cx,y1:cy,x2:x,y2:y,class:`map-edge ${tone}`});
      line.addEventListener('click',() => choose(first,person.id)); graph.append(line);
      const g = svg('g',{class:`map-person ${tone}`,role:'button',tabindex:0,'aria-label':`${person.name}: ${first.action}`,'aria-pressed':String(!!active),'data-person':person.id});
      g.append(svg('circle',{cx:x,cy:y,r:22}));
      const initials = person.name.split(/\s+/).slice(0,2).map(n => n[0]).join('');
      g.append(svg('text',{x,y:y+5,'text-anchor':'middle'},initials));
      const maxName = small.matches ? 21 : 28;
      g.append(svg('text',{x,y:y+42,'text-anchor':'middle'},person.name.length > maxName ? person.name.slice(0,maxName-2)+'…' : person.name));
      const action = small.matches ? ({'Exercise or conversion':'Exercise / conversion','Tax or exercise payment':'Tax / exercise payment'}[first.action] || first.action) : `${first.action} · ${person.events.length} ${person.events.length === 1 ? 'event' : 'events'}`;
      g.append(svg('text',{x,y:y+59,'text-anchor':'middle',class:'map-node-action'},action));
      g.append(svg('title',{},person.name));
      const activate = () => {choose(first,person.id); graph.querySelector(`[data-person="${CSS.escape(person.id)}"]`)?.focus({preventScroll:true});};
      g.addEventListener('click',activate); g.addEventListener('keydown',e => {if (['Enter',' '].includes(e.key)) {e.preventDefault();activate();}}); graph.append(g);
    });
    ring(graph, cx, cy);
    if (!people.length) graph.append(svg('text',{x:cx,y:320,'text-anchor':'middle',fill:'#96a49b','font-size':small.matches ? 12 : 16},'No people yet'));
    $('page').textContent = people.length ? `${page*size+1}–${page*size+shown.length} of ${people.length} people` : 'Saved SEC evidence';
    $('previous').disabled = page === 0; $('next').disabled = (page+1)*size >= people.length;
    const list = $('events'); list.replaceChildren();
    rows.forEach(e => {
      const button = make('button',null,'map-event'); button.type = 'button'; button.dataset.eventId = e.id; button.setAttribute('aria-pressed',String(e.id === selected));
      const main = make('span'); main.append(make('strong',`${names(e)} · ${e.action}`),make('small',`${e.basis} · Filed ${date(e.filed_at)}${e.amendment ? ' · Amendment' : ''}`));
      button.append(make('span','●',e.tone),main,make('span',amount(e),'event-money'));
      button.addEventListener('click',() => {choose(e); $('events').querySelector(`[data-event-id="${CSS.escape(e.id)}"]`)?.focus({preventScroll:true});}); list.append(button);
    });
    if (!rows.length) list.append(make('p','Saved events for this view will appear here.','map-note'));
    $('time').textContent = Number.isFinite(cutoff) ? date(new Date(cutoff).toISOString()) : 'Latest saved filing';
    $('time-slider').setAttribute('aria-valuetext',$('time').textContent);
    $('list-title').textContent = 'Reported filings';
    $('filter').parentElement.hidden = false; $('ownership-note').hidden = true;
    $('coverage').textContent = `${loaded} filings · ${rows.length} events`;
    source(event);
    if (restoreFocus && focusSelector) (root.querySelector(focusSelector) || graph.querySelector('[data-map-score-center]'))?.focus({preventScroll:true});
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
      cursor = data.next_cursor; loaded += data.loaded_filings; coverage = data.coverage;
      dates = [...new Set(events.map(e => Date.parse(e.filed_at)).filter(Number.isFinite))].sort((a,b) => a-b);
      const slider = $('time-slider'); slider.disabled = dates.length < 2; slider.max = Math.max(0,dates.length-1);
      slider.value = Number.isFinite(cutoff) ? Math.max(0,dates.indexOf(cutoff)) : slider.max;
      $('load').hidden = !cursor; $('load').textContent = 'Load older filings';
      $('status').textContent = coverage.filings ? 'Saved SEC filings' : 'No filings yet';
      render();
    } catch (_) { $('status').textContent = 'Please retry to load the saved filings.'; $('load').hidden = false; $('load').textContent = 'Retry loading filings'; }
    finally {pending = false; $('load').disabled = false;}
  }
  root.querySelectorAll('[data-map-view]').forEach(button => button.addEventListener('click',() => {
    view = button.dataset.mapView; page = 0; selected = null;
    root.querySelectorAll('[data-map-view]').forEach(b => b.setAttribute('aria-pressed',String(b === button))); render();
  }));
  $('filter').addEventListener('change',() => {filter = $('filter').value; page = 0; selected = null; render();});
  $('time-slider').addEventListener('input',() => {cutoff = dates[Number($('time-slider').value)]; page = 0; selected = null; render();});
  $('latest').addEventListener('click',() => {cutoff = Infinity; $('time-slider').value = $('time-slider').max; page = 0; selected = null; render();});
  $('previous').addEventListener('click',() => {page--;render();}); $('next').addEventListener('click',() => {page++;render();});
  $('load').addEventListener('click',load);
  $('score-return').addEventListener('click', overview);
  root.addEventListener('keydown', event => {if (event.key === 'Escape' && (pinned || selected)) {event.preventDefault(); overview();}});
  small.addEventListener('change',() => {page = 0;render();});
  screenNode?.addEventListener('rati:screen-detail', event => {
    const next = event.detail;
    if (next?.market !== 'stocks' || next.item?.id !== root.dataset.ticker) return;
    const changed = scoreData(item) !== scoreData(next.item);
    const timestamp = value => value.score_as_of || value.captured_at || value.event_at;
    const timeChanged = timestamp(item) !== timestamp(next.item);
    item = next.item;
    if (!changed && !timeChanged) return;
    scoreTime(timestamp(item));
    if (!changed) return;
    const graph = $('graph'), active = document.activeElement;
    const key = graph.contains(active) ? active.dataset.scoreKey : null;
    const restoreCenter = graph.contains(active) && active.hasAttribute('data-map-score-center');
    const preview = hovered || focused;
    hovered = null; focused = null;
    readScore();
    if (!positive.some(part => part.key === pinned)) pinned = null;
    graph.querySelectorAll('.map-score-track, .map-score-segment, [data-map-score-center]').forEach(el => el.remove());
    ring(graph, small.matches ? 180 : 380, small.matches ? 184 : 218);
    if (!selected || preview) context();
    if (key || restoreCenter) (graph.querySelector(`[data-score-key="${CSS.escape(key || '')}"]`) || graph.querySelector('[data-map-score-center]'))?.focus({preventScroll:true});
  });
  render();
  load();
})();
