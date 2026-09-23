(() => {
  'use strict';
  const data = document.getElementById('entityData');
  if (!data) return;
  const {wallet, entity} = JSON.parse(data.textContent);
  const graph = document.querySelector('[data-entity-map]');
  window.ratiOrbit?.attach(graph);
  const navigation = window.EntityMapNavigation.attach(graph);
  const small = matchMedia('(max-width:500px)');
  const svg = (tag, attrs, text) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key,value]) => element.setAttribute(key,value));
    if (text != null) element.textContent = text;
    return element;
  };
  const money = value => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:0}).format(value);
  function stockGlyph(stock) {
    const value = stock.indicator || {};
    const allowed = (value, options, fallback) => options.includes(value) ? value : fallback;
    const glyph = {
      band:allowed(value.band,[1,2,3],1),
      mix:allowed(value.mix_state,['available','zero','unknown'],'unknown'),
      sentimentMix:window.RatiRingGlyph.readSentiment(value.sentiment_mix),
      sentiment:allowed(value.sentiment,['positive','negative','neutral','unknown'],'unknown'),
      risk:allowed(value.risk,['none','detected','unknown'],'unknown'),
    };
    const slices = Array.isArray(value.slices) ? value.slices : [];
    let contributions = ['market','evidence','social'].map((key,index) => {
      const part = slices.find(part => part?.key === key);
      return {key,label:['Market','Filings + news','External social'][index],value:Number.isFinite(part?.value) ? Math.max(0,part.value) : 0};
    }).filter(part => part.value > 0);
    const total = contributions.reduce((sum,part) => sum + part.value,0);
    if (glyph.mix !== 'available' || !Number.isFinite(total) || total <= 0) contributions = [];
    contributions.forEach(part => {part.share = part.value/total;});
    return {glyph,contributions,description:value.description || 'Attention unavailable. Filing sentiment unknown. Risk factor checks unavailable.'};
  }
  function scoreWheel({glyph,contributions}, x, y, outer) {
    const width = small.matches ? 16 : 20;
    const geometry = {cx:0,cy:0,outer:outer*2,radius:outer*2-width/2,width,patternScale:1.5,markerScale:1.5};
    const wheel = svg('g', {class:'entity-score-ring map-glyph',transform:`translate(${x} ${y}) scale(0.5)`,'aria-hidden':'true'});
    window.RatiRingGlyph.drawFace({
      ringLayer:wheel,glyph,geometry,small:small.matches,contributions,toneLabel:'Filing sentiment',
      points:value=>`${value.toLocaleString('en-US',{maximumFractionDigits:2})} pts`,
      percent:part=>`${Math.round(part.share*100)}% of attention contributions`,
    });
    return wheel;
  }
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
      const href = person.wallet_id ? `/wallet/${encodeURIComponent(person.wallet_id)}` : `/wallets/stocks/${encodeURIComponent(stock.ticker)}/${encodeURIComponent(person.id)}`;
      const link = svg('a',{href,class:`map-interest ${tone}`,tabindex:0,'aria-label':label,'data-entity-interest':person.id,'data-entity-neighbours':stock.ticker,...(orbiting ? {'data-orbit-anchor':`${x},${y}`} : {})});
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
    const stocks = [...entity.stocks].sort((a,b) => {
      const left = Number.isFinite(a.value) ? a.value : -1;
      const right = Number.isFinite(b.value) ? b.value : -1;
      return right-left || a.ticker.localeCompare(b.ticker);
    });
    const rx = mobile ? 150 : 270, ry = 150;
    const spiral = stocks.length > 8;
    const scaleAt = index => spiral ? 1+index/12 : 1;
    const extent = scaleAt(Math.max(0,stocks.length-1)), margin = 96;
    graph.dataset.layout = spiral ? 'spiral' : 'ring';
    graph.dataset.orbitTrack = `${rx},${ry}`;
    navigation.fit({x:cx-rx*extent-margin,y:cy-ry*extent-margin,width:2*(rx*extent+margin),height:2*(ry*extent+margin)});
    if (spiral) navigation.zoom((rx*extent+margin)/(rx*scaleAt(7)+margin));
    const orbiting = true;
    // Use all loaded holdings for one stable size scale.
    const maximum = Math.max(0,...entity.stocks.map(stock => Number.isFinite(stock.value) ? Math.max(0,stock.value) : 0));
    const minimumSize = mobile ? 18 : 20, maximumSize = mobile ? 28 : 36;
    const edges = svg('g', {class:'entity-edges'});
    const nodes = svg('g', {class:'entity-stocks'});
    graph.append(edges,nodes);
    stocks.forEach((stock,index) => {
      const angle = -Math.PI/2 + index*Math.PI*2/(spiral ? 8 : Math.max(1,stocks.length));
      const scale = scaleAt(index);
      const x = cx + rx*scale*Math.cos(angle), y = cy + ry*scale*Math.sin(angle);
      const indicator = stockGlyph(stock);
      const holding = Number.isFinite(stock.value) && stock.value >= 0 ? stock.value : null;
      const share = holding !== null && maximum > 0 ? holding/maximum : 0;
      const outer = Math.sqrt(minimumSize**2 + (maximumSize**2-minimumSize**2)*share);
      const radius = outer + 3;
      const valueLabel = holding === null ? `${stock.events.length} ${stock.events.length === 1 ? 'event' : 'events'}` : money(holding);
      const description = `${stock.ticker}. ${indicator.description} ${holding === null ? valueLabel : "Reported holding value: " + valueLabel}.`;
      stock.events.forEach((event,i) => {
        const bend = (i-(stock.events.length-1)/2)*Math.min(8,48/Math.max(1,stock.events.length-1));
        const length = Math.hypot(x-cx,y-cy) || 1;
        const tone = event.action === 'Sold' ? 'sell' : event.action === 'Bought' || event.view === 'ownership' ? 'buy' : 'role';
        const line = svg('path',{d:`M ${cx} ${cy} Q ${(cx+x)/2-(y-cy)/length*bend} ${(cy+y)/2+(x-cx)/length*bend} ${x} ${y}`,class:`entity-edge ${tone}`,'vector-effect':'non-scaling-stroke',...(orbiting ? {'data-orbit':''} : {})});
        line.append(svg('title',{},`${stock.ticker} · ${event.action} · Filed ${event.filed_at?.slice(0,10) || ''}`)); edges.append(line);
      });
      const link = svg('a',{href:`/stock/${encodeURIComponent(stock.ticker)}`,class:'entity-stock',tabindex:0,'aria-label':description,'data-entity-stock':stock.ticker,...(orbiting ? {'data-orbit-anchor':`${x},${y}`} : {})});
      // One stock link contains the face and labels. The face uses the same
      // stock colors and markers as the row; holding value sets its size.
      link.append(svg('circle',{cx:x,cy:y,r:radius,class:'entity-glyph-backplate'}),scoreWheel(indicator,x,y,outer));
      link.append(svg('rect',{x:x-55,y:y+radius+2,width:110,height:mobile ? 60 : 54,class:'entity-label-hit'}));
      link.append(svg('text',{x,y:y+radius+(mobile ? 22 : 18),'text-anchor':'middle',class:'entity-stock-symbol'},stock.ticker));
      link.append(svg('text',{x,y:y+radius+(mobile ? 38 : 32),'text-anchor':'middle',class:'map-node-action'},valueLabel));
      link.append(svg('text',{x,y:y+radius+(mobile ? 54 : 48),'text-anchor':'middle',class:'map-sentiment-label'},indicator.glyph.sentimentMix.compact));
      link.append(svg('title',{},description));
      nodes.append(link);
      loadNeighbours(stock, {x, y, radius, cx, cy, orbiting});
    });
    const center = svg('g',{class:'entity-center','aria-label':wallet.name});
    center.append(svg('circle',{cx,cy,r:mobile ? 48 : 62,class:'map-center'}),svg('text',{x:cx,y:cy+7,'text-anchor':'middle','class':'map-center-text'},initialsOf(wallet.name)),svg('title',{},wallet.name)); graph.append(center);
  }
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
