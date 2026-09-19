(() => {
  'use strict';
  const root = document.querySelector('[data-token-replay]');
  if (!root) return;
  const $ = key => root.querySelector(`[data-replay-${key}]`);
  const graph = $('graph'), screen = document.getElementById('screenData');
  let item = (screen?.ratiScreenDetail || JSON.parse(screen?.textContent || '{}')).item || {};
  let data = null, page = 0, receiptBase = '/api/memecoins/evidence/', selected = null;
  let controller, timer;
  const small = matchMedia('(max-width:500px)');
  const make = (tag, text, cls) => {const el = document.createElement(tag); if (text != null) el.textContent = text; if (cls) el.className = cls; return el;};
  const svg = (tag, attrs, text) => {const el = document.createElementNS('http://www.w3.org/2000/svg',tag); Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k,v)); if (text != null) el.textContent = text; return el;};
  const score = () => Number.isFinite(item.score) ? String(Math.round(item.score)) : '—';
  const parts = () => [...(item.score_detail?.drivers || []), ...(item.score_detail?.penalties || []).map(p => ({...p,value:-Math.abs(p.value)}))].filter(p => Number.isFinite(p.value) && p.value !== 0);
  const tone = p => p.value < 0 ? 'var(--red)' : `var(--score-${p.key},var(--muted))`;
  const activate = (el, fn) => {el.addEventListener('click',fn); el.addEventListener('keydown',e => {if (['Enter',' '].includes(e.key)) {e.preventDefault();fn();}});};
  function overview(part) {
    selected = null; $('score-return').hidden = !part;
    root.querySelectorAll('[data-replay-event]').forEach(b => b.setAttribute('aria-pressed','false'));
    const panel = $('selection'); panel.replaceChildren(make('h3',score(),'map-score-heading'));
    if (part) panel.append(make('h4',part.label),make('p',`${part.value > 0 ? '+' : ''}${part.value} pts`));
    const list = make('ul',null,'map-score-legend');
    parts().forEach(p => {const li = make('li'), dot = make('span',null,'map-score-swatch'); dot.style.background = tone(p); li.append(dot,make('span',p.label),make('strong',`${p.value > 0 ? '+' : ''}${p.value} pts`)); list.append(li);});
    panel.append(list);
    if (item.score == null) panel.append(make('p','RATi score pending.','map-note'));
    document.dispatchEvent(new CustomEvent('rati:map-time',{detail:{time:null}}));
  }
  function details(node, eventId) {
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
    graph.replaceChildren(); graph.dataset.phase = 'settled';
    const cx = small.matches ? 180 : 380, cy = small.matches ? 184 : 218, r = small.matches ? 48 : 62, width = small.matches ? 16 : 20;
    graph.setAttribute('viewBox',small.matches ? '0 0 360 390' : '0 0 760 440');
    const frame = data?.frames.at(-1), size = small.matches ? 4 : 8;
    const all = (frame?.nodes || []).filter(n => n.id !== 'launch');
    const launchWallet = data?.launch?.wallet;
    all.sort((a,b) => Number(b.address === launchWallet)-Number(a.address === launchWallet));
    page = Math.min(page,Math.max(0,Math.ceil(all.length/size)-1));
    const shown = all.slice(page*size,page*size+size);
    const positions = new Map([['launch',{x:cx,y:cy}]]);
    shown.forEach((n,i) => positions.set(n.id,{x:small.matches ? (i%2 ? 278 : 82) : (i%2 ? 595 : 165),y:small.matches ? 66+Math.floor(i/2)*240 : 50+Math.floor(i/2)*103}));
    (frame?.edges || []).forEach(edge => {const a=positions.get(edge.source),b=positions.get(edge.target); if(a&&b) graph.append(svg('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:`map-edge ${edge.role === 'bought' ? 'up' : edge.role === 'sold' ? 'down' : ''}`}));});
    shown.forEach(n => {const {x,y}=positions.get(n.id), label=n.address === launchWallet ? 'Launch wallet' : n.kind; const g=svg('g',{class:'map-person',role:'button',tabindex:0,'data-node':n.id,'aria-label':`${n.kind}: ${n.address}`}); g.append(svg('circle',{cx:x,cy:y,r:22}),svg('text',{x,y:y+5,'text-anchor':'middle'},n.address.slice(0,4)),svg('text',{x,y:y+42,'text-anchor':'middle'},label),svg('title',{},n.address)); activate(g,()=>details(n)); graph.append(g);});
    graph.append(svg('circle',{cx,cy,r,class:'map-score-track','stroke-width':width}));
    const contributions=parts(),total=contributions.reduce((v,p)=>v+Math.abs(p.value),0); let angle=-Math.PI/2;
    contributions.forEach(p => {const sweep=Math.abs(p.value)/total*Math.PI*2,end=angle+sweep; const attrs={class:'map-score-segment',role:'button',tabindex:0,'stroke-width':width,'aria-label':`${p.label}: ${p.value} pts`}; const segment=contributions.length===1 ? svg('circle',{...attrs,cx,cy,r}) : svg('path',{...attrs,d:`M ${cx+r*Math.cos(angle)} ${cy+r*Math.sin(angle)} A ${r} ${r} 0 ${sweep>Math.PI?1:0} 1 ${cx+r*Math.cos(end)} ${cy+r*Math.sin(end)}`}); segment.style.stroke=tone(p); activate(segment,()=>overview(p)); graph.append(segment); angle=end;});
    const center=svg('g',{class:'map-score-center',role:'button',tabindex:0,'data-node':'launch','aria-label':`${item.name}, RATi score ${score()}. Show score overview.`});
    center.append(svg('circle',{cx,cy,r:r-width/2-3,class:'map-center'}),svg('text',{x:cx,y:cy-6,'text-anchor':'middle',class:'map-center-text'},item.name?.length > 10 ? item.name.slice(0,6)+'…' : item.name),svg('text',{x:cx,y:cy+16,'text-anchor':'middle',class:'map-center-score'},score())); activate(center,()=>overview()); graph.append(center);
    $('paging').hidden=all.length<=size; $('previous').disabled=page===0; $('next').disabled=(page+1)*size>=all.length; $('page').textContent=`${page*size+1}–${page*size+shown.length} of ${all.length}`;
  }
  const findings = () => (item.assessment?.drivers || []).filter(f => f.evidence?.length);
  function findingDetails(finding) {
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
  $('previous').addEventListener('click',()=>{page--;draw();});$('next').addEventListener('click',()=>{page++;draw();});$('score-return').addEventListener('click',()=>overview());
  root.addEventListener('keydown',e=>{if(e.key==='Escape'){overview();graph.querySelector('.map-score-center')?.focus();}});
  small.addEventListener('change',()=>{page=0;draw();});
  screen?.addEventListener('rati:screen-detail',e=>{if(e.detail?.market!=='memecoins'||e.detail.item?.id!==root.dataset.coinId)return;item=e.detail.item;draw();activity();if(selected?.startsWith('finding:') && !findings().some(f=>'finding:'+f.id===selected)){overview();graph.querySelector('.map-score-center')?.focus();}else if(!selected)overview();});
  window.addEventListener('pagehide',()=>{controller?.abort();clearTimeout(timer);});
  draw();overview();activity();load();
})();
