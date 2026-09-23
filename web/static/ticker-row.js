(() => {
  const chartCache = new Map();
  const annotationCache = new Map();

  function esc(value) {
    const node = document.createElement('span');
    node.textContent = String(value ?? '');
    return node.innerHTML;
  }

  function attr(value) {
    return esc(value).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function number(value) {
    if (value === null || value === undefined || value === '') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function money(value) {
    const parsed = number(value);
    if (parsed === null) return '—';
    if (parsed < 1) return '$' + parsed.toFixed(4);
    if (parsed < 10) return '$' + parsed.toFixed(3);
    return '$' + parsed.toFixed(2);
  }

  function percent(value) {
    const parsed = number(value);
    if (parsed === null) return '—';
    return `${parsed >= 0 ? '+' : ''}${parsed.toFixed(1)}%`;
  }

  function ago(value) {
    if (!value) return '';
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (seconds < 90) return 'now';
    if (seconds < 5400) return Math.round(seconds / 60) + 'm ago';
    if (seconds < 129600) return Math.round(seconds / 3600) + 'h ago';
    return Math.round(seconds / 86400) + 'd ago';
  }

  function status(row) {
    if (row.section === 'cases') {
      const confidence = number(row.case_confidence);
      return [confidence === null ? 'VIEW' : `${Math.round(confidence * 100)}%`, 'thesis'];
    }
    const tradeState = String(row.trade_state || '').toUpperCase();
    if (tradeState && tradeState !== 'UNKNOWN') {
      return [tradeState, `state-${tradeState.toLowerCase()}`];
    }
    const stage = String(row.stage || '').toUpperCase();
    if (row.section === 'scored' && stage) return [stage, `stage-${stage.toLowerCase()}`];
    const sessions = {regular: 'REG', pre: 'PRE', after: 'AH', overnight: 'OVN'};
    if (row.session && sessions[row.session]) return [sessions[row.session], 'session'];
    if (row.source === 'sec') return ['SEC', 'source'];
    if (row.source === 'quiet') return ['WATCH', 'quiet'];
    return ['', ''];
  }

  function thesisCue(row) {
    const thesis = row.directional_thesis;
    const direction = String(thesis?.direction || '').toLowerCase();
    if (!['up', 'down', 'flat'].includes(direction)) {
      if (row.source !== 'market' && row.section !== 'scored') return ['', ''];
      return ['', 'Directional thesis: model learning'];
    }
    const arrows = {up: '↑', down: '↓', flat: '↔'};
    const label = String(thesis.label || (direction === 'flat' ? 'No edge' : direction));
    const horizon = String(thesis.horizon || '60m');
    const description = `Directional thesis: ${label}, ${horizon}`;
    return [
      `<small class="ticker-thesis ticker-thesis-${direction}" title="${attr(description)}" aria-hidden="true"><b>${arrows[direction]}</b><span>${esc(label.toUpperCase())} · ${esc(horizon.toUpperCase())}</span></small>`,
      description,
    ];
  }

  function scoredComparison(row) {
    if (row.section !== 'scored') return '';
    const values = [
      ['RANK', number(row.custom_rank) === null ? '—' : `#${Math.round(number(row.custom_rank))}`],
      ['PULSE', number(row.score) === null ? '—' : Math.round(number(row.score))],
      ['SETUP', number(row.setup_score) === null ? '—' : Math.round(number(row.setup_score))],
      ['RVOL', number(row.relative_volume) === null ? '—' : `${number(row.relative_volume).toFixed(1)}×`],
      ['15M', percent(row.momentum_15m_pct)],
    ];
    return `<span class="ticker-comparison" aria-hidden="true">${values.map(([label, value]) => {
      const tone = label === '15M' && number(row.momentum_15m_pct) !== null
        ? (number(row.momentum_15m_pct) >= 0 ? ' up' : ' down')
        : '';
      return `<span><small>${label}</small><b class="${tone.trim()}">${esc(value)}</b></span>`;
    }).join('')}</span>`;
  }

  function renderShell({
    href,
    ariaLabel,
    coinTone = 0,
    coinLabel,
    headline,
    headlineMeta = '',
    age = '',
    ageLive = false,
    company,
    detailMarkup = '',
    catalyst,
    catalystTone = '',
    catalystMarkup = '',
    quoteValue,
    chartMarkup,
    quoteMarkup,
    quoteTone = 'flat',
    updated = false,
    dataTicker = '',
    dataSportsGame = '',
  }) {
    const rowClass = `${updated ? ' is-updated' : ''}${dataSportsGame ? ' sports-pulse-row' : ''}`;
    const rowData = dataTicker
      ? ` data-ticker-row="${attr(dataTicker)}"`
      : dataSportsGame
        ? ` data-sports-pulse-row="${attr(dataSportsGame)}"`
        : '';
    const safeCatalystTone = ['gap', 'risk'].includes(catalystTone) ? ` ${catalystTone}` : '';
    const safeQuoteTone = ['up', 'down', 'flat'].includes(quoteTone) ? quoteTone : 'flat';
    return `<a class="token-row ticker-row${rowClass}" href="${attr(href)}"${rowData} aria-label="${attr(ariaLabel)}">
      <span class="coin coin-${Number(coinTone) || 0}"><b>${esc(coinLabel)}</b><i></i></span>
      <span class="token-copy">
        <span class="ticker-line"><strong>${esc(headline)}</strong>${headlineMeta}<small class="ticker-age${ageLive ? ' ticker-age-live' : ''}">${esc(age)}</small></span>
        <span class="company-name">${esc(company)}</span>
        ${detailMarkup}
        <span class="catalyst${safeCatalystTone}">${esc(catalyst)}${catalystMarkup}</span>
      </span>
      <span class="quote">
        <strong>${esc(quoteValue)}</strong>
        ${chartMarkup}
        <small class="${safeQuoteTone}">${quoteMarkup}</small>
      </span>
    </a>`;
  }

  function render(row, options = {}) {
    const change = number(row.change_pct);
    const changeClass = change === null ? 'flat' : change >= 0 ? 'up' : 'down';
    const company = row.company || row.name || row.ticker;
    const [statusLabel, statusTone] = status(row);
    const badge = statusLabel
      ? `<small class="ticker-badge ticker-badge-${statusTone}">${esc(statusLabel)}</small>`
      : '';
    const [thesisCueMarkup, thesisLabel] = thesisCue(row);
    const marketFreshness = row.section === 'scored' || row.source === 'market';
    const age = ago(marketFreshness
      ? row.quote_time || row.event_at || row.entered_at
      : row.entered_at || row.event_at);
    const ageLive = row.mark_source === 'quote' && Number(row.mark_age_seconds) < 120;
    const events = Number(row.event_count) > 1
      ? `<span class="event-count">+${Number(row.event_count) - 1}</span>`
      : '';
    const rugValue = number(row.rug_score);
    const rugLevel = String(row.rug_level || 'unknown').toLowerCase();
    const tradeState = String(row.trade_state || '').toUpperCase();
    let safety = '';
    if (rugValue !== null && ['high', 'critical'].includes(rugLevel)) {
      safety = `<span class="rug-count rug-${attr(rugLevel)}">HIGH RISK</span>`;
    } else if (row.section === 'scored' && rugValue === null) {
      safety = '<span class="rug-count rug-unknown">RISK UNKNOWN</span>';
    }
    const catalystTone = row.sentiment === 'risk' ? ' risk' : row.sentiment === 'gap' ? ' gap' : '';
    const updated = options.updated ?? row.has_update;
    const updateClass = updated ? ' is-updated' : '';
    const tradeStateLabel = tradeState && tradeState !== 'UNKNOWN' && tradeState !== statusLabel
      ? `, ${tradeState}`
      : '';
    const marketLabel = `${statusLabel ? `, ${statusLabel}` : ''}${tradeStateLabel}${rugValue !== null ? `, rug risk ${rugValue.toFixed(0)}` : ''}`;
    const scoreLabel = row.section === 'scored'
      ? `, rank ${number(row.custom_rank) ?? 'unknown'}, Pulse score ${number(row.score) ?? 'unknown'}, setup ${number(row.setup_score) ?? 'unknown'}, relative volume ${number(row.relative_volume) ?? 'unknown'}, 15 minute momentum ${percent(row.momentum_15m_pct)}`
      : '';
    const label = `${row.ticker}, ${company}, ${money(row.price)}, ${percent(row.change_pct)}${marketLabel}${scoreLabel}${thesisLabel ? `, ${thesisLabel}` : ''}`;
    const thesis = row.section === 'cases' && row.case_thesis
      ? `<span class="case-thesis">${esc(row.case_thesis)}</span>`
      : '';
    const caseSource = row.section === 'cases' && row.case_source_name
      ? `<span class="case-source">Shared by ${esc(row.case_source_name)}</span>`
      : '';
    const caseSocial = row.section === 'cases' && row.social_label
      ? `<span class="case-social">${esc(row.social_label)}</span>`
      : '';
    const trackPrompt = row.needs_thesis
      ? '<span class="case-track-prompt">Comment once to make this view personal</span>'
      : '';
    const comparison = scoredComparison(row);
    return renderShell({
      href: `/stock/${encodeURIComponent(row.ticker)}`,
      ariaLabel: label,
      coinTone: row.coin_tone,
      coinLabel: row.coin_label || String(row.ticker).slice(0, 2),
      headline: row.ticker,
      headlineMeta: badge,
      age,
      ageLive,
      company,
      detailMarkup: `${caseSource}${thesis}${caseSocial}${trackPrompt}${comparison}`,
      catalyst: row.pulse_label || 'No recent event',
      catalystTone: catalystTone.trim(),
      catalystMarkup: `${events}${thesisCueMarkup}${safety}`,
      quoteValue: money(row.price),
      chartMarkup: `<svg class="mini-chart" data-ticker="${attr(row.ticker)}" viewBox="0 0 64 18" preserveAspectRatio="none" aria-hidden="true"><path class="chart-placeholder" d="M1 12 L13 10 L25 13 L39 8 L51 10 L63 7"/></svg>`,
      quoteMarkup: `<span class="quote-period">Session</span> ${esc(percent(row.change_pct))}`,
      quoteTone: changeClass,
      updated: Boolean(updateClass),
      dataTicker: row.ticker,
    });
  }

  // Reject invalid samples and order by actual time. Missing prices are not zero;
  // an unavailable series is not a flat price and never gets a decorative zigzag.
  function chartRows(points) {
    const ordered = new Map();
    (Array.isArray(points) ? points : []).forEach(point => {
      if (!point || typeof point.price === 'boolean' || !point.time) return;
      const time = Date.parse(point.time), price = number(point.price);
      if (Number.isFinite(time) && price !== null && price > 0) ordered.set(time, price);
    });
    return [...ordered].sort((a, b) => a[0] - b[0]).map(([time, price]) => ({time, price}));
  }

  function sharedChartDomain() {
    let maximum = 0;
    chartCache.forEach(points => {
      const rows = chartRows(points), baseline = rows[0]?.price;
      if (!baseline) return;
      rows.forEach(point => {
        const move = Math.abs((point.price / baseline - 1) * 100);
        if (Number.isFinite(move)) maximum = Math.max(maximum, move);
      });
    });
    return Math.max(2, Math.ceil(maximum / 2 - 1e-9) * 2);
  }

  function chartElement(tag, attributes, text) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function chartSummary(svg, summary) {
    const label = `${svg.dataset.ticker || 'Stock'}: ${summary}`;
    svg.removeAttribute('aria-hidden');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', label);
    svg.prepend(chartElement('title', {}, label));
  }

  function unavailableChart(svg) {
    svg.classList.remove('loaded', 'rising', 'falling', 'flat');
    svg.classList.add('unavailable');
    svg.dataset.history = 'unavailable';
    svg.replaceChildren(chartElement('text', {class:'mini-chart-empty', x:32, y:12, 'text-anchor':'middle'}, '—'));
    chartSummary(svg, 'price history unavailable');
  }

  function drawMiniChart(svg, points, annotations = [], domain = sharedChartDomain()) {
    const rows = chartRows(points);
    if (!rows.length) { unavailableChart(svg); return; }
    const baseline = rows[0].price;
    const values = rows.map(point => (point.price / baseline - 1) * 100);
    if (values.some(value => !Number.isFinite(value))) { unavailableChart(svg); return; }
    const start = rows[0].time, end = rows.at(-1).time;
    const x = time => start === end ? 32 : 1 + (time - start) / (end - start) * 62;
    const y = value => 9 - (value / domain) * 7;
    const delta = values.at(-1);
    svg.classList.toggle('rising', delta > 0);
    svg.classList.toggle('falling', delta < 0);
    svg.classList.toggle('flat', delta === 0);
    svg.replaceChildren();
    if (rows.length === 1) {
      svg.append(chartElement('circle', {class:'mini-chart-point', cx:32, cy:9, r:1.8, fill:'currentColor'}));
    } else {
      const path = rows.map((point, index) => `${index ? 'L' : 'M'}${x(point.time).toFixed(2)} ${y(values[index]).toFixed(2)}`).join(' ');
      svg.append(
        chartElement('line', {class:'mini-chart-zero', x1:1, y1:9, x2:63, y2:9}),
        chartElement('path', {class:'mini-chart-line', d:path, fill:'none', stroke:'currentColor', 'stroke-width':1.4, 'vector-effect':'non-scaling-stroke', 'stroke-linecap':'round', 'stroke-linejoin':'round'}),
      );
    }
    const entry = (Array.isArray(annotations) ? annotations : []).filter(item => item?.type === 'pulse_entry').at(-1);
    const entryTime = Date.parse(entry?.time);
    if (Number.isFinite(entryTime) && entryTime >= start && entryTime <= end) {
      let index = 0;
      rows.forEach((point, i) => { if (Math.abs(point.time - entryTime) < Math.abs(rows[index].time - entryTime)) index = i; });
      svg.append(
        chartElement('line', {class:'pulse-entry-line', x1:x(rows[index].time), y1:1, x2:x(rows[index].time), y2:17}),
        chartElement('circle', {class:'pulse-entry-dot', cx:x(rows[index].time), cy:y(values[index]), r:2.4}),
      );
    }
    svg.classList.remove('unavailable');
    svg.classList.add('loaded');
    svg.dataset.history = 'available';
    const scope = rows.length === 1 ? `one saved price ${money(baseline)} at ${new Date(start).toISOString()}` :
      `saved price history ${money(baseline)} to ${money(rows.at(-1).price)}, ${percent(delta)} from ${new Date(start).toISOString()} to ${new Date(end).toISOString()}`;
    chartSummary(svg, scope);
  }

  let chartRequest = null, chartRefreshFailed = false;
  function paintCharts(root = document) {
    const domain = sharedChartDomain();
    root.querySelectorAll('.mini-chart[data-ticker]').forEach(svg => {
      drawMiniChart(svg, chartCache.get(svg.dataset.ticker), annotationCache.get(svg.dataset.ticker), domain);
      if (chartRefreshFailed && svg.classList.contains('loaded')) {
        svg.dataset.history = 'stale';
        const label = svg.getAttribute('aria-label') + '. Refresh unavailable; showing saved history.';
        svg.setAttribute('aria-label', label);
        svg.querySelector('title').textContent = label;
      }
    });
  }

  // Read the board in bounded pages. Keep saved charts until every page arrives.
  // Coalesce refreshes so one list update cannot start several chart walks.
  function loadCharts(url) {
    if (chartRequest) return chartRequest;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 25000);
    chartRequest = (async () => {
      try {
        const nextCharts = new Map(), nextAnnotations = new Map();
        let offset = 0, pages = 0, hasMore = false;
        do {
          const target = new URL(url, window.location.href);
          if (offset) target.searchParams.set('offset', String(offset));
          const response = await fetch(target, {signal:controller.signal});
          if (!response.ok || response.redirected) throw new Error('Chart refresh unavailable');
          const data = await response.json();
          if (!data || !data.charts || typeof data.charts !== 'object' || Array.isArray(data.charts)) throw new Error('Invalid chart payload');
          Object.entries(data.charts).slice(0, 50).forEach(([ticker, points]) => nextCharts.set(ticker, points));
          Object.entries(data.annotations || {}).forEach(([ticker, annotations]) => {
            if (nextCharts.has(ticker)) nextAnnotations.set(ticker, annotations);
          });
          hasMore = data.has_more === true;
          if (hasMore) {
            const nextOffset = Number(data.next_offset);
            if (!Number.isInteger(nextOffset) || nextOffset <= offset || ++pages >= 20) {
              throw new Error('Invalid chart page');
            }
            offset = nextOffset;
          }
        } while (hasMore);
        chartCache.clear(); annotationCache.clear();
        nextCharts.forEach((points, ticker) => chartCache.set(ticker, points));
        nextAnnotations.forEach((annotations, ticker) => annotationCache.set(ticker, annotations));
        chartRefreshFailed = false;
      } catch (_) { chartRefreshFailed = true; }
      finally { clearTimeout(timer); chartRequest = null; paintCharts(); }
    })();
    return chartRequest;
  }

  window.TickerRow = Object.freeze({ago, loadCharts, paintCharts, render, renderShell});
})();
