(() => {
  'use strict';
  const dataNode = document.getElementById('memecoinPageData');
  if (!dataNode) return;
  let page;
  try { page = JSON.parse(dataNode.textContent); } catch (_) { return; }
  const find = (selector) => document.querySelector(selector);
  const put = (selector, value) => { const node = find(selector); if (node) node.textContent = value; };
  const finite = (value) => typeof value === 'number' && Number.isFinite(value);
  const percent = (value) => finite(value) ? `${value >= 0 ? '+' : ''}${value.toFixed(2)}%` : 'Pending';
  const tone = (value) => finite(value) ? (value >= 0 ? 'up' : 'down') : 'flat';
  const time = (value) => { const date = new Date(value); return value && Number.isFinite(date.getTime()) ? `${date.toISOString().slice(0, 16).replace('T', ' ')} UTC` : 'Source time pending'; };
  const price = (value) => {
    if (!finite(value)) return 'unknown';
    if (value === 0) return '$0';
    return `$${value.toLocaleString('en-US', {maximumSignificantDigits: 7, maximumFractionDigits: 20})}`;
  };
  const stale = (coin) => {
    const stamp = Date.parse(coin.observed_at);
    return Boolean(coin.stale) || !Number.isFinite(stamp) || Date.now() - stamp > 900000 || stamp - Date.now() > 300000;
  };
  const element = (tag, text, className) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; };
  const showStatus = (selector, message) => { const node = find(selector); if (node) { node.textContent = message; node.hidden = !message; } };
  const coinPath = (coin) => `/memecoins/coin/${encodeURIComponent(coin.id || coin.coin_id)}`;
  const detailHref = (coin) => {
    const url = new URL(coinPath(coin), location.origin);
    const market = page.market || {};
    url.searchParams.set('q', market.query || '');
    url.searchParams.set('sort', market.sort || 'volume');
    url.searchParams.set('view', find('[data-list-view]')?.dataset.listView || 'pulse');
    return `${url.pathname}${url.search}`;
  };
  const marketMessage = (market) => {
    if (market.status === 'disabled') return 'Collection paused. Showing saved market data.';
    if (market.status === 'stale' || market.rows.some(stale)) return 'Saved prices · Quote updates are delayed.';
    if (market.refresh_failed) return 'Saved prices · The source refresh will retry shortly.';
    return '';
  };
  function renderMarket(market) {
    const list = find('[data-coin-list]');
    if (!list) return;
    const focused = list.contains(document.activeElement) ? document.activeElement?.closest('[data-coin-id]')?.dataset.coinId : null;
    const fragment = document.createDocumentFragment();
    market.rows.forEach((coin, index) => {
      const old = stale(coin);
      const row = element('a', undefined, 'token-row ticker-row meme-row');
      row.href = detailHref(coin); row.dataset.coinId = coin.id;
      row.setAttribute('aria-label', `${coin.name}, ${coin.symbol}, price ${coin.price_label}, 24-hour change ${finite(coin.change_24h) ? percent(coin.change_24h) : 'unknown'}, volume ${coin.volume_label}, market cap ${coin.market_cap_label}${old ? ', stale quote' : ''}`);
      const badge = element('span', undefined, `coin coin-${index % 5}`); badge.setAttribute('aria-hidden', 'true');
      badge.append(element('b', coin.symbol.slice(0, 3)), element('i'));
      const copy = element('span', undefined, 'token-copy');
      const headline = element('span', undefined, 'ticker-line'); headline.append(element('strong', coin.symbol));
      if (old) headline.append(element('small', 'Stale', 'meme-stale'));
      copy.append(headline, element('span', coin.name, 'company-name'), element('span', `Vol ${coin.volume_label} · Cap ${coin.market_cap_label}`, 'meme-row-stats'), element('small', time(coin.observed_at), 'meme-row-time'));
      const quote = element('span', undefined, 'quote');
      quote.append(element('strong', coin.price_label), element('small', finite(coin.change_24h) ? percent(coin.change_24h) : '—', tone(coin.change_24h)), element('span', '24h', 'meme-period'));
      row.append(badge, copy, quote); fragment.append(row);
    });
    list.replaceChildren(fragment);
    if (focused) Array.from(list.children).find((row) => row.dataset.coinId === focused)?.focus({preventScroll: true});
    put('[data-coin-count]', `${market.rows.length} of ${market.total} coins`);
    put('[data-market-updated]', market.collected_at ? `Collected ${time(market.collected_at)}` : 'First collection pending');
    showStatus('[data-market-status]', marketMessage(market));
    const empty = find('[data-coin-empty]');
    empty.hidden = market.rows.length > 0;
    if (!market.rows.length) {
      const messages = {
        disabled: ['Memecoin feed paused', 'The feed will return when collection resumes.'],
        unavailable: ['Waiting for CoinGecko', 'The source refresh will retry shortly.'],
        pending: ['First prices are on the way', 'The next collection will fill this list.']
      };
      const message = messages[market.status] || ['Try another name or symbol', `Search covers the ${market.total} coins in this snapshot.`];
      empty.replaceChildren(element('strong', message[0]), element('p', message[1]));
      if (!messages[market.status]) { const link = element('a', 'Show all coins'); link.href = find('[data-list-path]').dataset.listPath; empty.append(link); }
    }
    find('[data-desktop-list]')?.dispatchEvent(new CustomEvent('desktop-rows-rendered', {bubbles: true}));
  }
  function renderCalls(calls) {
    const list = find('[data-coin-calls]');
    if (!list) return;
    const fragment = document.createDocumentFragment();
    calls.forEach((call) => {
      const row = element('li'); row.dataset.callId = call.public_id;
      const header = element('header'); const link = element('a'); link.href = coinPath(call);
      link.append(element('strong', call.symbol), element('span', call.name));
      header.append(link, element('b', percent(call.return_pct), tone(call.return_pct)));
      const who = element('p'); const caller = element('a', call.caller_handle); caller.href = `/u/${encodeURIComponent(call.caller_handle)}?market=memecoins`;
      const closed = call.status === 'closed'; who.append(caller, document.createTextNode(` · ${closed ? 'Closed' : 'Open'}`));
      const facts = element('dl');
      [['Entry', call.entry_price_label], [closed ? 'Exit' : 'Current', closed ? call.exit_price_label : call.mark_price_label]].forEach(([label, value]) => { const fact = element('div'); fact.append(element('dt', label), element('dd', value || 'Pending')); facts.append(fact); });
      row.append(header, who, facts, element('small', `Opened ${time(call.entry_at)}${closed && call.exit_at ? ` · Closed ${time(call.exit_at)}` : ''}`)); fragment.append(row);
    });
    list.replaceChildren(fragment);
    const empty = find('[data-calls-empty]'); if (empty) empty.hidden = calls.length > 0;
    const active = calls.find((call) => call.public_id === page.active_call_id);
    const activeReturn = find('[data-active-call-return]');
    if (active && activeReturn) { activeReturn.textContent = percent(active.return_pct); activeReturn.className = tone(active.return_pct); }
    find('[data-desktop-list]')?.dispatchEvent(new CustomEvent('desktop-rows-rendered', {bubbles: true}));
  }
  function renderChart(history) {
    const chart = find('[data-coin-chart]');
    if (!chart) return;
    const samples = new Map();
    (history || []).forEach((point) => { const stamp = Date.parse(point.observed_at); if (Number.isFinite(stamp) && finite(point.price) && point.price > 0) samples.set(stamp, point.price); });
    const points = Array.from(samples, ([stamp, value]) => ({stamp, value})).sort((a, b) => a.stamp - b.stamp);
    chart.replaceChildren(); chart.toggleAttribute('hidden', points.length < 2);
    const axis = find('[data-chart-axis]'); axis.replaceChildren();
    if (points.length < 2) { put('[data-chart-status]', points.length ? 'One saved price. The next source observation will start the chart.' : 'Collecting price history. Saved source observations will appear here.'); return; }
    const first = points[0], last = points[points.length - 1];
    const low = Math.min(...points.map((p) => p.value)), high = Math.max(...points.map((p) => p.value));
    const xy = (point) => [20 + (point.stamp - first.stamp) / (last.stamp - first.stamp) * 560, high === low ? 110 : 200 - (point.value - low) / (high - low) * 180];
    const svg = (tag, attrs) => { const node = document.createElementNS('http://www.w3.org/2000/svg', tag); Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value)); return node; };
    for (const y of [20, 110, 200]) chart.append(svg('line', {x1: 20, x2: 580, y1: y, y2: y, stroke: 'var(--line)', 'stroke-width': 1}));
    let segment = [], gaps = false;
    const draw = () => { if (segment.length > 1) chart.append(svg('polyline', {points: segment.map((p) => xy(p).join(',')).join(' '), fill: 'none', stroke: 'var(--green)', 'stroke-width': 2, 'vector-effect': 'non-scaling-stroke'})); segment = []; };
    points.forEach((point, index) => {
      if (index && point.stamp - points[index - 1].stamp > 600000) { draw(); gaps = true; }
      segment.push(point);
      const [cx, cy] = xy(point); const dot = svg('circle', {cx, cy, r: 3, fill: 'var(--green)'}); const title = svg('title', {}); title.textContent = `${time(point.stamp)} · ${price(point.value)}`; dot.append(title); chart.append(dot);
    }); draw();
    chart.setAttribute('aria-label', `${points.length} recorded prices from ${time(first.stamp)} to ${time(last.stamp)}. Low ${price(low)}, high ${price(high)}.${gaps ? ' Gaps mark periods without a saved observation.' : ''}`);
    put('[data-chart-status]', `${points.length} source observations · Low ${price(low)} · High ${price(high)}${gaps ? ' · Gaps in collection' : ''}`);
    axis.append(element('span', time(first.stamp)), element('span', time(last.stamp)));
  }
  let actionPending = false;
  function renderDetail(detail) {
    const coin = detail.coin; const old = stale(coin);
    put('[data-coin-price]', coin.price_label);
    const change = find('[data-coin-change]'); change.textContent = finite(coin.change_24h) ? percent(coin.change_24h) : '—'; change.className = tone(coin.change_24h);
    put('[data-quote-time]', time(coin.observed_at)); put('[data-coin-volume]', coin.volume_label); put('[data-coin-cap]', coin.market_cap_label);
    document.querySelectorAll('[data-coin-metric]').forEach((node) => { const key = node.dataset.coinMetric, value = coin[key]; node.textContent = key.endsWith('24h') ? price(value) : finite(value) ? value.toLocaleString('en-US', {maximumFractionDigits: 2}) : 'unknown'; });
    put('[data-evidence-time]', time(coin.observed_at)); put('[data-collected-time]', time(detail.collected_at));
    let message = '';
    if (detail.status === 'disabled') message = 'Collection paused · Showing the saved quote.';
    else if (old || detail.status === 'stale') message = 'Saved quote · Waiting for a fresh source time.';
    else if (!detail.in_current_snapshot) message = 'Saved coin · Outside the latest top-100 snapshot.';
    else if (detail.refresh_failed) message = 'Saved quote · The source refresh will retry shortly.';
    showStatus('[data-detail-status]', message);
    const canCall = !old && detail.status === 'ok' && detail.can_call !== false;
    const action = find('[data-coin-call-endpoint]'); if (action) action.disabled = actionPending || !canCall;
    if (!canCall) put('[data-call-status]', 'A fresh source quote is required to open or close a Call.');
    else if (!actionPending && find('[data-call-status]')?.textContent === 'A fresh source quote is required to open or close a Call.') put('[data-call-status]', '');
    renderChart(detail.history);
    renderCalls(detail.calls || page.calls || []);
  }
  let refreshing = false;
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    const buttons = document.querySelectorAll('[data-coin-refresh]'); buttons.forEach((button) => { button.disabled = true; button.textContent = 'Refreshing…'; });
    try {
      let url;
      if (page.kind === 'market') { const params = new URLSearchParams({q: page.market.query || '', sort: page.market.sort || 'volume'}); url = `/api/memecoins?${params}`; }
      else if (page.kind === 'detail') url = `/api/memecoins/${encodeURIComponent(page.detail.coin.id)}`;
      else url = '/api/memecoin-calls';
      const response = await fetch(url, {credentials: 'same-origin', headers: {Accept: 'application/json'}});
      if (!response.ok) throw new Error('Saved updates are delayed. Try Refresh again shortly.');
      const data = await response.json();
      if (page.kind === 'market') { if (!Array.isArray(data.rows)) throw new Error('Saved updates are delayed. Try Refresh again shortly.'); page.market = data; renderMarket(data); }
      else if (page.kind === 'detail') { if (!data.coin) throw new Error('Saved updates are delayed. Try Refresh again shortly.'); page.detail = data; renderDetail(data); }
      else { if (!Array.isArray(data.calls)) throw new Error('Saved updates are delayed. Try Refresh again shortly.'); page.calls = data.calls; renderCalls(data.calls); put('[data-alpha-status]', 'Calls updated'); }
    } catch (_) {
      if (page.kind === 'market') renderMarket(page.market);
      if (page.kind === 'detail') renderDetail(page.detail);
      showStatus(page.kind === 'market' ? '[data-market-status]' : page.kind === 'detail' ? '[data-detail-status]' : '[data-alpha-status]', 'Saved updates are delayed. Try Refresh again shortly.');
    } finally { refreshing = false; buttons.forEach((button) => { button.disabled = false; button.textContent = 'Refresh'; }); }
  }
  document.querySelectorAll('[data-coin-refresh]').forEach((button) => button.addEventListener('click', refresh));
  find('[data-coin-call-endpoint]')?.addEventListener('click', async (event) => {
    const button = event.currentTarget; if (actionPending || button.disabled) return;
    actionPending = true; button.disabled = true; put('[data-call-status]', 'Saving paper Call…');
    try {
      const response = await fetch(button.dataset.coinCallEndpoint, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', Accept: 'application/json'}, body: '{}'});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The Call could not be saved. Try again shortly.');
      location.reload();
    } catch (error) { put('[data-call-status]', error.message || 'The Call could not be saved. Try again shortly.'); actionPending = false; renderDetail(page.detail); }
  });
  find('[data-share-coin]')?.addEventListener('click', async () => {
    const url = new URL(coinPath(page.detail.coin), location.origin).href;
    try {
      if (navigator.share) { await navigator.share({title: `${page.detail.coin.symbol} · RATi Memecoins`, url}); put('[data-share-status]', 'Share complete'); }
      else if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(url); put('[data-share-status]', 'Coin link copied'); }
      else put('[data-share-status]', `Coin link: ${url}`);
    } catch (error) { if (error.name !== 'AbortError') put('[data-share-status]', `Coin link: ${url}`); }
  });
  if (page.kind === 'market') renderMarket(page.market);
  if (page.kind === 'detail') renderDetail(page.detail);
  setInterval(() => { if (!document.hidden) refresh(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
})();
