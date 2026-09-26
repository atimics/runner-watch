(() => {
  'use strict';
  const root = document.querySelector('[data-token-replay]');
  if (!root) return;
  const $ = key => root.querySelector(`[data-replay-${key}]`);
  const graph = $('graph'), screen = document.getElementById('screenData');
  window.ratiOrbit?.attach(graph);
  const navigation = window.EntityMapNavigation.attach(graph);
  let item = (screen?.ratiScreenDetail || JSON.parse(screen?.textContent || '{}')).item || {};
  let data = null, fitted = null, receiptBase = '/api/memecoins/evidence/', selected = null;
  let controller, timer, selectedPart = null;
  let glyph = {}, contributions = [], controls = [], score = '—';
  const small = matchMedia('(max-width:500px)');
  const make = (tag, text, cls) => {const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el;};
  const svg = (tag, attrs, text) => {const el = document.createElementNS('http://www.w3.org/2000/svg',tag); Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k,v)); if (text != null) el.textContent = text; return el;};
  const number = value => value.toLocaleString('en-US', {maximumFractionDigits:2});
  const points = value => `${number(value)} pts`;
  const percent = part => `${number(part.share * 100)}% of attention contributions`;
  const activate = (el, fn) => {el.addEventListener('click',fn); el.addEventListener('keydown',e => {if (['Enter',' '].includes(e.key)) {e.preventDefault();fn();}});};
  function readGlyph() {
    const value = item.indicator || {};
    const allowed = (value, values, fallback) => values.includes(value) ? value : fallback;
    const finite = value => typeof value === 'number' && Number.isFinite(value);
    glyph = {
      band:allowed(value.band,[1,2,3],1),
      mix:allowed(value.mix_state,['available','zero','unknown'],'unknown'),
      sentimentMix:window.RatiRingGlyph.readSentiment(value.sentiment_mix),
      sentiment:allowed(value.sentiment,['positive','negative','neutral','unknown'],'unknown'),
      risk:allowed(value.risk,['none','detected','significant','unknown'],'unknown'),
    };
    score = finite(value.score) ? number(value.score) : '—';
    const slices = Array.isArray(value.slices) ? value.slices : [];
    contributions = ['market','evidence','social'].map((key,index) => {
      const part = slices.find(part => part?.key === key);
      return {key,label:['Market','Chain evidence','External social'][index],value:finite(part?.value) ? Math.max(0,part.value) : 0};
    }).filter(part => part.value > 0);
    const total = contributions.reduce((sum,part) => sum + part.value,0);
    if (glyph.mix !== 'available' || !Number.isFinite(total) || total <= 0) contributions = [];
    contributions.forEach(part => {part.share = part.value / total;});
    controls = [...contributions,{key:'sentiment',label:'Chain evidence tone'}];
    if (glyph.risk !== 'none') controls.push({key:'risk',label:'Risk factors'});
    if (!controls.some(part => part.key === selectedPart)) selectedPart = null;
  }
  function overview(part) {
    root.querySelector('.map-workspace').classList.add('map-overview');
    selected = null; selectedPart = part?.key || null; $('score-return').hidden = !part;
    root.querySelectorAll('[data-replay-event], [data-finding-id]').forEach(b => b.setAttribute('aria-pressed','false'));
    graph.querySelectorAll('[data-score-key]').forEach(b => b.setAttribute('aria-pressed',String(b.dataset.scoreKey === selectedPart)));
    const panel = $('selection'); panel.replaceChildren();
    panel.append(make('p',`Attention ${score === '—' ? 'unavailable' : score + ' points'} · Chain evidence tone: ${glyph.sentimentMix.reading} · ${window.RatiRingGlyph.riskReading(glyph.risk)}`,'map-glyph-reading'));
    if (part) {
      panel.append(make('h4',part.label));
      if (part.key === 'risk') panel.append(make('p',`${window.RatiRingGlyph.riskReading(glyph.risk)}.`));
      if (part.key === 'risk' && Array.isArray(item.indicator?.risk_factors) && item.indicator.risk_factors.length) {
        const factors = make('ul',null,'map-risk-factors');
        item.indicator.risk_factors.forEach(reason => factors.append(make('li',reason))); panel.append(factors);
      }
      if (part.key === 'sentiment') panel.append(make('p',`${glyph.sentimentMix.reading}. ${glyph.sentimentMix.basis}.`));
      else if (part.key !== 'risk') panel.append(make('p',`${points(part.value)} · ${percent(part)}`,'map-score-breakdown'));
    }
    const list = make('ul',null,'map-score-legend');
    contributions.forEach(p => {const li = make('li'), dot = make('span',null,'map-score-swatch'); dot.style.background = `var(--indicator-${p.key})`; dot.dataset.pattern = p.key; dot.setAttribute('aria-hidden','true'); li.append(dot,make('span',p.label),make('strong',points(p.value))); list.append(li);});
    if (list.children.length) panel.append(list);
    else panel.append(make('p',glyph.mix === 'zero' ? 'Saved attention contributions total zero.' : 'Attention breakdown is awaiting a saved assessment.','map-glyph-empty'));
    if (item.assessment?.reason) panel.append(make('p',item.assessment.reason));
    document.dispatchEvent(new CustomEvent('rati:map-time',{detail:{time:null}}));
  }
  function clearPart() {
    selectedPart = null;
    graph.querySelectorAll('[data-score-key]').forEach(b => b.setAttribute('aria-pressed','false'));
  }
  function wireControl(element, part) {
    element.setAttribute('role','button'); element.setAttribute('tabindex','0');
    element.dataset.scoreKey = part.key;
    element.setAttribute('aria-pressed',String(selectedPart === part.key));
    activate(element,()=>overview(part));
    element.addEventListener('keydown',event => {
      if (!['ArrowRight','ArrowDown','ArrowLeft','ArrowUp','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const targets = [...graph.querySelectorAll('[data-score-key]')], index = targets.indexOf(element);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? targets.length - 1 : (index + (['ArrowRight','ArrowDown'].includes(event.key) ? 1 : -1) + targets.length) % targets.length;
      targets[next].focus({preventScroll:true});
    });
  }
  function details(node, eventId) {
    clearPart();
    root.querySelector('.map-workspace').classList.remove('map-overview');
    selected = node.id; $('score-return').hidden = false;
    const panel = $('selection'); panel.replaceChildren(make('h3',node.id === 'launch' ? item.name : node.kind === 'wallet' ? 'Wallet' : node.kind));
    if (node.id !== 'launch') panel.append(make('p',node.address));
    const edges = data.frames.at(-1).edges.filter(e => eventId ? e.event_id === eventId : e.source === node.id || e.target === node.id);
    const seen = new Set();
    edges.forEach(edge => {
      if (seen.has(edge.event_id)) return; seen.add(edge.event_id);
      const event = data.events.find(e => e.event_id === edge.event_id);
      const link = make('a',`${edge.role} · slot ${edge.slot} ↗`); link.href = receiptBase + encodeURIComponent(edge.signature); link.target = '_blank'; link.rel = 'noopener noreferrer'; panel.append(link);
      if (event?.net_token_amount) panel.append(make('p',`${event.net_token_amount} tokens · ${event.amount_basis}`));
      (event?.balances || []).forEach(b => panel.append(make('p',`${b.raw} raw units / ${b.decimals} decimals of ${b.mint}`)));
      if (event?.observed_at) {panel.append(make('p',`Recorded ${event.observed_at}`)); if (eventId) document.dispatchEvent(new CustomEvent('rati:map-time',{detail:{time:event.observed_at,label:'Chain event'}}));}
    });
    root.querySelectorAll('[data-replay-event]').forEach(b => b.setAttribute('aria-pressed',String(b.dataset.replayEvent === eventId)));
  }
  function draw() {
    const active = graph.contains(document.activeElement) ? document.activeElement : null;
    const focusKey = active?.dataset.scoreKey, focusNode = active?.dataset.node;
    readGlyph();
    graph.replaceChildren(); graph.dataset.phase = 'settled';
    const {cx,cy} = window.RatiRingGlyph.metrics(glyph,small.matches);
    const frame = data?.frames.at(-1);
    const all = (frame?.nodes || []).filter(n => n.id !== 'launch');
    const launchWallet = data?.launch?.wallet;
    all.sort((a,b) => Number(b.address === launchWallet)-Number(a.address === launchWallet));
    // Every wallet sits on the stock map's ring, never a page of it. Past
    // eight the ring becomes the wallet map's spiral, opened on its first turn.
    const rx = small.matches ? 150 : 270, ry = 150, spiral = all.length > 8;
    const scaleAt = index => spiral ? 1+index/12 : 1;
    const extent = scaleAt(Math.max(0,all.length-1)), margin = 96;
    graph.dataset.orbitCenter = `${cx},${cy}`;
    graph.dataset.orbitTrack = `${rx},${ry}`;
    graph.dataset.layout = spiral ? 'spiral' : 'ring';
    // Live score refreshes redraw the map; keep the reader's zoom unless the layout changed.
    const layout = `${small.matches}:${all.length}`;
    if (layout !== fitted) {
      fitted = layout;
      navigation.fit({x:cx-rx*extent-margin,y:cy-ry*extent-margin,width:2*(rx*extent+margin),height:2*(ry*extent+margin)});
      if (spiral) navigation.zoom((rx*extent+margin)/(rx*scaleAt(7)+margin));
    }
    const positions = new Map([['launch',{x:cx,y:cy}]]);
    all.forEach((n,i) => {
      const angle = -Math.PI/2 + i*Math.PI*2/(spiral ? 8 : Math.max(1,all.length)), scale = scaleAt(i);
      positions.set(n.id,{x:cx+rx*scale*Math.cos(angle),y:cy+ry*scale*Math.sin(angle)});
    });
    (frame?.edges || []).forEach(edge => {const a=positions.get(edge.source),b=positions.get(edge.target); if(a&&b) graph.append(svg('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:`map-edge ${edge.role === 'bought' ? 'up' : edge.role === 'sold' ? 'down' : ''}`,'vector-effect':'non-scaling-stroke','data-orbit':''}));});
    all.forEach(n => {const {x,y}=positions.get(n.id), label=n.address === launchWallet ? 'Launch wallet' : n.kind; const g=svg('g',{class:'map-person',role:'button',tabindex:0,'data-node':n.id,'aria-label':`${n.kind}: ${n.address}`,'data-orbit-anchor':`${x},${y}`}); g.append(svg('circle',{cx:x,cy:y,r:22}),svg('text',{x,y:y+5,'text-anchor':'middle'},n.address.slice(0,4)),svg('text',{x,y:y+42,'text-anchor':'middle'},label),svg('title',{},n.address)); activate(g,()=>details(n)); graph.append(g);});
    const ringLayer = svg('g',{class:'map-ring map-glyph'});
    window.RatiRingGlyph.draw({
      ringLayer, graph, glyph, small:small.matches, contributions, controls, score,
      name:item.name || 'Token', label:item.name?.length > 13 ? item.name.slice(0,6)+'…'+item.name.slice(-6) : item.name,
      toneLabel:'Chain evidence tone', centerAttributes:{'data-node':'launch'},
      points, percent, wireControl, overview:()=>overview(),
    });
    graph.append(ringLayer);
    if (active) {
      const target = focusKey ? graph.querySelector(`[data-score-key="${CSS.escape(focusKey)}"]`) : focusNode ? graph.querySelector(`[data-node="${CSS.escape(focusNode)}"]`) : null;
      (target || graph.querySelector('.map-score-center'))?.focus({preventScroll:true});
    }
  }
  const findings = () => (item.assessment?.drivers || []).filter(f => f.evidence?.length);
  function findingDetails(finding) {
    clearPart();
    root.querySelector('.map-workspace').classList.remove('map-overview');
    selected = 'finding:' + finding.id; $('score-return').hidden = false;
    const panel = $('selection'); panel.replaceChildren(make('h3',finding.label),make('p',finding.explanation));
    const list = make('ul',null,'map-receipts');
    (finding.evidence || []).forEach((receipt,i) => {
      const row = make('li');
      [[receipt.receipt_url,`${receipt.kind} ${i+1} ↗`],[receipt.source_url,'Explorer ↗']].forEach(([href,label]) => {
        if (!href) return;
        try {const url = new URL(href,location.origin); if (!['https:','http:'].includes(url.protocol)) return;
          const link=make('a',label); link.href=url.href;link.target='_blank';link.rel='noopener noreferrer';row.append(link);
        } catch (_) { /* A valid saved URL is required. */ }
      });
      list.append(row);
    });
    panel.append(list);
    document.dispatchEvent(new CustomEvent('rati:map-time',{detail:{time:null}}));
  }
  function activity() {
    const list=$('events'); list.replaceChildren(); const frame=data?.frames.at(-1),seen=new Set();
    [...(frame?.edges || [])].reverse().forEach(edge => {if(seen.has(edge.event_id))return;seen.add(edge.event_id); const event=data.events.find(e=>e.event_id===edge.event_id),node=frame.nodes.find(n=>n.id===edge.source); if(!node)return; const button=make('button',null,'map-event');button.type='button';button.dataset.replayEvent=edge.event_id;button.setAttribute('aria-pressed','false'); const text=make('span');text.append(make('strong',`${edge.role} · ${(event.wallet||node.address).slice(0,6)}…`),make('small',`Slot ${edge.slot}${event.observed_at?' · '+event.observed_at:''}`));button.append(make('span','●',edge.role==='sold'?'down':edge.role==='bought'?'up':''),text);button.addEventListener('click',()=>details(node,edge.event_id));list.append(button);});
    findings().forEach(f => {const button=make('button',null,'map-event');button.type='button';button.dataset.findingId=f.id;button.setAttribute('aria-pressed',String(selected==='finding:'+f.id));const text=make('span');text.append(make('strong',f.label),make('small',`${f.evidence.length} receipts`));button.append(make('span','●'),text);button.addEventListener('click',()=>findingDetails(f));list.append(button);});
    if(!seen.size && !findings().length)list.append(make('p','Saved chain events will appear here.','map-note'));
  }
  async function load() {
    controller?.abort();controller=new AbortController();
    const revision=new URL(location.href).searchParams.get('replay');
    try {const res=await fetch(`/api/memecoins/${encodeURIComponent(root.dataset.coinId)}/replay${revision?'?revision='+encodeURIComponent(revision):''}`,{signal:controller.signal,headers:{Accept:'application/json'}});if(!res.ok)throw Error('Please retry to load saved chain events.');const record=await res.json();if(record.status!=='ready'){$('status').textContent=record.message || 'Saved chain events will appear here.';timer=setTimeout(load,15000);return;}data=record.payload;receiptBase=record.receipt_base||'/api/memecoins/evidence/';$('status').textContent='';draw();activity();}catch(e){if(e.name!=='AbortError')$('status').textContent=e.message;}
  }
  $('score-return').addEventListener('click',()=>overview());
  root.addEventListener('keydown',e=>{if(e.key==='Escape'){overview();graph.querySelector('.map-score-center')?.focus();}});
  small.addEventListener('change',draw);
  screen?.addEventListener('rati:screen-detail',e=>{if(e.detail?.market!=='memecoins'||e.detail.item?.id!==root.dataset.coinId)return;item=e.detail.item;draw();activity();if(selected?.startsWith('finding:') && !findings().some(f=>'finding:'+f.id===selected)){overview();graph.querySelector('.map-score-center')?.focus();}else if(!selected)overview(controls.find(part=>part.key===selectedPart));});
  window.addEventListener('pagehide',()=>{controller?.abort();clearTimeout(timer);});
  draw();overview();activity();load();
})();
