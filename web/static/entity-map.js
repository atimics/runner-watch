(() => {
  'use strict';
  const data = document.getElementById('entityData');
  if (!data) return;
  const {wallet, entity} = JSON.parse(data.textContent);
  const graph = document.querySelector('[data-entity-map]');
  const small = matchMedia('(max-width:500px)');
  const svg = (tag, attrs, text) => {
    const element = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key,value]) => element.setAttribute(key,value));
    if (text != null) element.textContent = text;
    return element;
  };
  const money = value => new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:0}).format(value);
  let page = 0;
  function draw() {
    graph.replaceChildren();
    const mobile = small.matches, cx = mobile ? 180 : 380, cy = mobile ? 184 : 218;
    const size = mobile ? 4 : 8;
    page = Math.min(page, Math.max(0, Math.ceil(entity.stocks.length/size)-1));
    const stocks = entity.stocks.slice(page*size,(page+1)*size);
    graph.setAttribute('viewBox', mobile ? '0 0 360 390' : '0 0 760 440');
    const maximum = Math.max(0,...entity.stocks.map(stock => stock.value || 0));
    stocks.forEach((stock,index) => {
      const x = mobile ? (index%2 ? 275 : 85) : (index%2 ? 580 : 180);
      const y = mobile ? 64+Math.floor(index/2)*210 : 62+Math.floor(index/2)*103;
      const radius = stock.value == null ? 20 : Math.sqrt(225+675*(maximum ? stock.value/maximum : 0));
      stock.events.forEach((event,i) => {
        const bend = (i-(stock.events.length-1)/2)*Math.min(8,48/Math.max(1,stock.events.length-1));
        const length = Math.hypot(x-cx,y-cy);
        const tone = event.action === 'Sold' ? 'sell' : event.action === 'Bought' || event.view === 'ownership' ? 'buy' : 'role';
        const line = svg('path',{d:`M ${cx} ${cy} Q ${(cx+x)/2-(y-cy)/length*bend} ${(cy+y)/2+(x-cx)/length*bend} ${x} ${y}`,class:`entity-edge ${tone}`});
        line.append(svg('title',{},`${stock.ticker} · ${event.action} · Filed ${event.filed_at?.slice(0,10) || ''}`)); graph.append(line);
      });
      const link = svg('a',{href:`/t/${encodeURIComponent(stock.ticker)}`,class:'map-person',tabindex:0,'aria-label':`${stock.ticker}, ${stock.events.length} events`, 'data-entity-stock':stock.ticker});
      link.append(svg('circle',{cx:x,cy:y,r:radius}),svg('text',{x,y:y+5,'text-anchor':'middle'},stock.ticker),svg('text',{x,y:y+radius+18,'text-anchor':'middle',class:'map-node-action'},stock.value == null ? `${stock.events.length} events` : money(stock.value)));
      graph.append(link);
    });
    const center = svg('g',{class:'entity-center','aria-label':wallet.name});
    const initials = wallet.name.split(/\s+/).slice(0,2).map(word => word[0]).join('');
    center.append(svg('circle',{cx,cy,r:mobile ? 48 : 62,class:'map-center'}),svg('text',{x:cx,y:cy+7,'text-anchor':'middle',class:'map-center-text'},initials),svg('title',{},wallet.name)); graph.append(center);
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
