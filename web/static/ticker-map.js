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
  let events = [], dates = [], cursor = null, loaded = 0, coverage = {}, selected = null;
  let view = 'all', filter = 'all', cutoff = Infinity, page = 0, pending = false;
  const small = window.matchMedia('(max-width:500px)');
  const pageSize = () => small.matches ? 4 : 8;
  const subset = () => events.filter(e => (Date.parse(e.filed_at) || 0) <= cutoff &&
    (filter === 'all' || (filter === 'other' ? !['Bought','Sold'].includes(e.action) : e.action === filter)));
  function source(event) {
    const panel = $('selection'); panel.replaceChildren();
    if (!event) {
      panel.append(make('h3', 'Select a filing'));
      document.dispatchEvent(new CustomEvent('rati:map-time', {detail:{time:null}})); return;
    }
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
    selected = event?.id || null;
    const people = peopleFor(subset()), at = people.findIndex(p => p.id === (personId || event?.people[0]?.id));
    if (at >= 0) page = Math.floor(at / pageSize());
    render();
  }
  function render() {
    const rows = subset(), people = peopleFor(rows), graph = $('graph');
    const event = rows.find(e => e.id === selected) || rows[0]; selected = event?.id || null;
    const size = pageSize(), cx = small.matches ? 180 : 380, cy = small.matches ? 165 : 218;
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
    graph.append(svg('circle',{cx,cy,r:small.matches ? 36 : 48,class:'map-center'}),svg('text',{x:cx,y:cy+7,'text-anchor':'middle',class:'map-center-text'},root.dataset.ticker));
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
  small.addEventListener('change',() => {page = 0;render();});
  load();
})();
