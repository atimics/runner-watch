(() => {
  'use strict';
  const money = value => Number.isFinite(value)
    ? new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:0}).format(value)
    : '—';
  const count = (value, singular, plural = `${singular}s`) => `${value} ${value === 1 ? singular : plural}`;
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  document.querySelectorAll('[data-cluster-worth]').forEach(root => {
    const selector = root.querySelector('[data-cluster-stock]');
    const result = root.querySelector('[data-cluster-result]');
    const retry = root.querySelector('[data-cluster-retry]');
    const cache = new Map();
    let controller;
    if (selector) {
      const options = [...selector.options].sort((a,b) => Number(b.dataset.holding)-Number(a.dataset.holding) || a.value.localeCompare(b.value));
      selector.replaceChildren(...options);
      selector.value = options[0].value;
    }
    function render(payload) {
      const value = element('strong',money(payload.value),'cluster-total');
      const basis = element('p',payload.basis,'cluster-basis');
      const coverage = element('p',`${count(payload.entity_count,'linked entity','linked entities')} · ${count(payload.stock_count,'stock')} · ${payload.covered} of ${payload.tracked} holdings valued`,'cluster-coverage');
      result.replaceChildren(value,basis,coverage);
      if (payload.prices_from && payload.prices_to) {
        const from = payload.prices_from.slice(0,10), to = payload.prices_to.slice(0,10);
        result.append(element('p',`Price dates: ${from}${from === to ? '' : ' – '+to}`,'cluster-dates'));
      }
      if (!payload.entity_count) {
        result.append(element('p','Linked entities appear as filings arrive.','cluster-status'));
        return;
      }
      if (payload.covered < payload.tracked) {
        result.append(element('p','The total covers holdings with clear share counts and saved prices.','cluster-status'));
      }
      const members = element('details',null,'cluster-breakdown');
      members.append(element('summary','Entities in this cluster'));
      const memberList = element('ul');
      payload.members.forEach(member => {
        const row = element('li');
        const label = element('div');
        const link = element('a',member.name);
        link.href = `/wallets/stocks/${encodeURIComponent(payload.ticker)}/${encodeURIComponent(member.id)}`;
        label.append(link,element('small',`${member.covered} of ${member.tracked} holdings valued`));
        row.append(label,element('strong',money(member.value)));
        memberList.append(row);
      });
      members.append(memberList);
      const stocks = element('details',null,'cluster-breakdown');
      stocks.append(element('summary','Stocks in this cluster'));
      const stockList = element('ul');
      payload.stocks.forEach(stock => {
        const row = element('li');
        const label = element('div');
        const link = element('a',stock.ticker);
        link.href = `/stock/${encodeURIComponent(stock.ticker)}`;
        label.append(link,element('small',`${stock.covered} of ${stock.tracked} holdings valued`));
        row.append(label,element('strong',money(stock.value)));
        stockList.append(row);
      });
      stocks.append(stockList);
      result.append(members,stocks);
    }
    async function load() {
      controller?.abort();
      const request = new AbortController();
      controller = request;
      const ticker = selector?.value || root.dataset.ticker;
      root.querySelector('[data-cluster-symbol]').textContent = ticker;
      retry.hidden = true;
      if (cache.has(ticker)) {render(cache.get(ticker)); return;}
      const status = element('p','Loading saved holdings…','cluster-status');
      status.setAttribute('role','status');
      result.replaceChildren(status);
      try {
        const response = await fetch(`/api/stocks/${encodeURIComponent(ticker)}/cluster-worth`,{headers:{Accept:'application/json'},signal:request.signal});
        if (!response.ok) throw new Error('cluster');
        const payload = await response.json();
        if (controller !== request) return;
        cache.set(ticker,payload);
        render(payload);
      } catch (error) {
        if (controller !== request || error.name === 'AbortError') return;
        status.textContent = 'Saved holdings are taking longer to load.';
        retry.hidden = false;
      }
    }
    selector?.addEventListener('change',load);
    retry.addEventListener('click',load);
    load();
  });
})();
