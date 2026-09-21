(() => {
  'use strict';
  const data = document.getElementById('entityData');
  if (!data) return;
  const {wallet, entity} = JSON.parse(data.textContent);
  const graph = document.querySelector('[data-entity-map]');
  window.ratiOrbit?.attach(graph);
  const small = matchMedia('(max-width:500px)');
  const svg = (tag, attrs, text) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key,value]) => element.setAttribute(key,value));
    if (text != null) element.textContent = text;
    return element;
  };
  const money = value => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:0}).format(value);
  function scoreWheel(stock, x, y, radius) {
    const wheel = svg('g', {class:'entity-score-ring',role:'img','aria-label':`Score ${Math.round(stock.score)}`});
    wheel.append(svg('circle',{cx:x,cy:y,r:radius,class:'entity-score-track'}));
    const valid = rows => Array.isArray(rows) ? rows.filter(p => p && Number.isFinite(p.value) && p.value !== 0) : [];
    const drivers = valid(stock.score_detail?.drivers);
    const parts = [...drivers.filter(p => p.value > 0), ...valid(stock.score_detail?.penalties).map(p => ({...p,value:-Math.abs(p.value)})), ...drivers.filter(p => p.value < 0)];
    const total = parts.reduce((sum,p) => sum+Math.abs(p.value),0);
    let angle = -Math.PI/2;
    parts.forEach(part => {
      const end = angle+Math.abs(part.value)/total*Math.PI*2;
      const attrs = {class:'entity-score-segment','data-score-key':part.key};
      const segment = parts.length === 1 ? svg('circle',{...attrs,cx:x,cy:y,r:radius}) : svg('path',{...attrs,d:`M ${x+radius*Math.cos(angle)} ${y+radius*Math.sin(angle)} A ${radius} ${radius} 0 ${end-angle > Math.PI ? 1 : 0} 1 ${x+radius*Math.cos(end)} ${y+radius*Math.sin(end)}`});
      segment.style.stroke = part.value < 0 ? 'var(--red)' : `var(--score-${part.key},var(--muted))`;
      segment.append(svg('title',{},`${part.label}: ${part.value > 0 ? '+' : ''}${part.value} pts`));
      wheel.append(segment); angle = end;
    });
    return wheel;
  }
  let page = 0;
  const initialsOf = name => String(name || '').split(/\s+/).filter(Boolean).slice(0,2).map(word => word[0]).join('').toUpperCase();
  /* The second hop: the other wallets that reported the same stock. That list
     lives on the stock itself, not just in this wallet's own filings, so it is
     fetched once per ticker and kept for the session: the stock map fans a
     wallet's other tickers the same way. */
  const neighbours = new Map();
  const NEIGHBOUR_TTL_MS = 5*60*1000;
  const toneOf = rows => rows.some(event => event.action === 'Sold')
    ? 'sell'
    : rows.some(event => event.action === 'Bought' || event.view === 'ownership') ? 'buy' : 'role';
  const neighbourPeople = payload => {
    const people = new Map();
    (payload?.events || []).forEach(event => (event.people || []).forEach(person => {
      if (!person || person.id === wallet.id) return;
      const existing = people.get(person.id);
      // The dot's colour is the whole label, so keep the strongest signal.
      const tone = toneOf([event]);
      if (!existing) { people.set(person.id, {...person, tone}); return; }
      if (existing.tone === 'role' && tone !== 'role') existing.tone = tone;
    }));
    return [...people.values()];
  };
  function loadNeighbours(stock, node) {
    const cached = neighbours.get(stock.ticker);
    if (cached) {
      drawNeighbours(cached.people, stock, node);
      if (Date.now() - cached.at < NEIGHBOUR_TTL_MS) return;
    }
    fetch(`/api/stocks/${encodeURIComponent(stock.ticker)}/map`, {headers:{Accept:'application/json'}})
      .then(response => response.ok ? response.json() : Promise.reject(new Error('holders')))
      .then(payload => {
        const people = neighbourPeople(payload);
        if (!people.length) { neighbours.delete(stock.ticker); return; }
        neighbours.set(stock.ticker, {at:Date.now(), people});
        drawNeighbours(people, stock, node);
      })
      .catch(() => {});
  }
  function drawNeighbours(people, stock, {x, y, radius, cx, cy, orbiting}) {
    graph.querySelectorAll(`[data-entity-neighbours="${CSS.escape(stock.ticker)}"]`).forEach(node => node.remove());
    const shown = people.slice(0, 4);
    if (!shown.length) return;
    const heading = Math.atan2(y - cy, x - cx) + Math.PI/2;
    const spread = shown.length === 1 ? 0 : 0.85;
    shown.forEach((person, index) => {
      const angle = shown.length === 1 ? heading : heading + (index/(shown.length-1) - 0.5)*spread;
      const orbit = radius + 20, dot = 4;
      // Start on the node's rim and stop on the dot's rim: the line never runs
      // under a label or through a circle.
      const sx = x + radius*Math.cos(angle), sy = y + radius*Math.sin(angle);
      const hx = x + orbit*Math.cos(angle), hy = y + orbit*Math.sin(angle);
      const ex = hx - dot*Math.cos(angle), ey = hy - dot*Math.sin(angle);
      const tone = person.tone || 'role';
      const label = `${person.name} also reported ${stock.ticker}`;
      const link = svg('a',{href:`/wallets/stocks/${encodeURIComponent(stock.ticker)}/${encodeURIComponent(person.id)}`,class:`map-interest ${tone}`,tabindex:0,'aria-label':label,'data-entity-interest':person.id,'data-entity-neighbours':stock.ticker,...(orbiting ? {'data-orbit-anchor':`${x},${y}`} : {})});
      // Colour carries the meaning; the name stays in the tooltip and the label.
      link.append(svg('path',{d:`M ${sx} ${sy} L ${ex} ${ey}`,class:`map-interest-edge ${tone}`}));
      link.append(svg('circle',{cx:hx,cy:hy,r:6,class:'map-interest-hit'}),svg('circle',{cx:hx,cy:hy,r:dot,class:'map-interest-dot'}),svg('title',{},label));
      graph.append(link);
    });
  }
  function draw() {
    graph.replaceChildren();
    const mobile = small.matches, cx = mobile ? 180 : 380, cy = mobile ? 184 : 218;
    graph.dataset.orbitCenter = `${cx},${cy}`;
    const size = mobile ? 4 : 8;
    page = Math.min(page, Math.max(0, Math.ceil(entity.stocks.length/size)-1));
    const stocks = entity.stocks.slice(page*size,(page+1)*size);
    graph.setAttribute('viewBox', mobile ? '0 0 360 390' : '0 0 760 440');
    // The same ring the stock map uses: stocks orbit the wallet on one ellipse
    // and travel along it, so the ring never swings off the canvas.
    if (mobile) delete graph.dataset.orbitTrack;
    else graph.dataset.orbitTrack = '270,150';
    const orbiting = !mobile;
    const maximum = Math.max(0,...entity.stocks.map(stock => stock.value || 0));
    stocks.forEach((stock,index) => {
      const angle = -Math.PI/2 + index*Math.PI*2/Math.max(1,stocks.length);
      const x = mobile ? (index%2 ? 275 : 85) : cx + 270*Math.cos(angle);
      const y = mobile ? 64+Math.floor(index/2)*210 : cy + 150*Math.sin(angle);
      const scored = Number.isFinite(stock.score);
      const radius = Math.max(scored ? 24 : 0, stock.value == null ? 20 : Math.sqrt(225+675*(maximum ? stock.value/maximum : 0)));
      stock.events.forEach((event,i) => {
        const bend = (i-(stock.events.length-1)/2)*Math.min(8,48/Math.max(1,stock.events.length-1));
        const length = Math.hypot(x-cx,y-cy) || 1;
        const tone = event.action === 'Sold' ? 'sell' : event.action === 'Bought' || event.view === 'ownership' ? 'buy' : 'role';
        const line = svg('path',{d:`M ${cx} ${cy} Q ${(cx+x)/2-(y-cy)/length*bend} ${(cy+y)/2+(x-cx)/length*bend} ${x} ${y}`,class:`entity-edge ${tone}`,'vector-effect':'non-scaling-stroke',...(orbiting ? {'data-orbit':''} : {})});
        line.append(svg('title',{},`${stock.ticker} · ${event.action} · Filed ${event.filed_at?.slice(0,10) || ''}`)); graph.append(line);
      });
      const link = svg('a',{href:`/t/${encodeURIComponent(stock.ticker)}`,class:'map-person',tabindex:0,'aria-label':`${stock.ticker}, ${scored ? `score ${Math.round(stock.score)}, ` : ''}${stock.events.length} events`, 'data-entity-stock':stock.ticker,...(orbiting ? {'data-orbit-anchor':`${x},${y}`} : {})});
      // No labels on the map itself: the ring carries the score and the node
      // opens the full detail on click. Hovering still names it.
      link.append(svg('circle',{cx:x,cy:y,r:radius}));
      if (scored) link.append(scoreWheel(stock,x,y,radius+3));
      link.append(
        svg('title',{},`${stock.ticker}${scored ? ` · score ${Math.round(stock.score)}` : ''}` +
          `${stock.value == null ? ` · ${stock.events.length} events` : ` · ${money(stock.value)}`}`)
      );
      graph.append(link);
      loadNeighbours(stock, {x, y, radius, cx, cy, orbiting});
    });
    const center = svg('g',{class:'entity-center','aria-label':wallet.name});
    center.append(svg('circle',{cx,cy,r:mobile ? 48 : 62,class:'map-center'}),svg('text',{x:cx,y:cy+7,'text-anchor':'middle','class':'map-center-text'},initialsOf(wallet.name)),svg('title',{},wallet.name)); graph.append(center);
    document.querySelector('[data-entity-paging]').hidden = entity.stocks.length <= size;
    document.querySelector('[data-entity-page]').textContent = `${page*size+1}–${page*size+stocks.length} of ${entity.stocks.length} stocks`;
    document.querySelector('[data-entity-previous]').disabled = page === 0;
    document.querySelector('[data-entity-next]').disabled = (page+1)*size >= entity.stocks.length;
  }
  document.querySelector('[data-entity-previous]').addEventListener('click',()=>{page--;draw();});
  document.querySelector('[data-entity-next]').addEventListener('click',()=>{page++;draw();});
  small.addEventListener('change',draw); draw();
  const chart = document.querySelector('.entity-worth-chart');
  const points = entity.series;
  const known = points.filter(point => point.value != null);
  if (!known.length) {document.querySelector('[data-worth-pending]').hidden = false; return;}
  chart.removeAttribute('hidden');
  const minTime = Date.parse(points[0].time), maxTime = Date.parse(points.at(-1).time);
  const maxValue = Math.max(1,...known.map(point => point.value));
  const x = point => maxTime === minTime ? 380 : 20+720*(Date.parse(point.time)-minTime)/(maxTime-minTime);
  const y = point => 160-140*point.value/maxValue;
  let previous = null;
  points.forEach(point => {
    if (point.value == null) {previous=null; return;}
    if (previous && previous.covered === point.covered && previous.tracked === point.tracked) {
      chart.append(svg('path',{d:`M ${x(previous)} ${y(previous)} H ${x(point)} V ${y(point)}`,class:'chart-line'}));
    }
    const dot = svg('circle',{cx:x(point),cy:y(point),r:4,class:'entity-worth-point'});
    dot.append(svg('title',{},`${point.time.slice(0,10)} · ${money(point.value)} · ${point.covered} stocks valued`)); chart.append(dot);
    previous = point;
  });
  document.querySelector('[data-worth-date]').textContent = `${points[0].time.slice(0,10)} — ${points.at(-1).time.slice(0,10)} · Filing dates`;
})();
