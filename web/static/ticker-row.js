(() => {
  const chartCache = new Map();
  const annotationCache = new Map();

  function esc(value) {
    const node = document.createElement('span');
    node.textContent = String(value ?? '');
    return node.innerHTML;
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
      const description = 'Directional thesis: model learning';
      return [
        `<small class="ticker-thesis ticker-thesis-learning" title="${description}" aria-hidden="true"><b>—</b><span>MODEL LEARNING</span></small>`,
        description,
      ];
    }
    const arrows = {up: '↑', down: '↓', flat: '↔'};
    const label = String(thesis.label || (direction === 'flat' ? 'No edge' : direction));
    const horizon = String(thesis.horizon || '60m');
    const description = `Directional thesis: ${label}, ${horizon}`;
    return [
      `<small class="ticker-thesis ticker-thesis-${direction}" title="${esc(description)}" aria-hidden="true"><b>${arrows[direction]}</b><span>${esc(label.toUpperCase())} · ${esc(horizon.toUpperCase())}</span></small>`,
      description,
    ];
  }

  function renderShell({
    href,
    ariaLabel,
    coinTone = 0,
    coinLabel,
    headline,
    headlineMeta = '',
    age = '',
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
      ? ` data-ticker-row="${esc(dataTicker)}"`
      : dataSportsGame
        ? ` data-sports-pulse-row="${esc(dataSportsGame)}"`
        : '';
    const safeCatalystTone = ['gap', 'risk'].includes(catalystTone) ? ` ${catalystTone}` : '';
    const safeQuoteTone = ['up', 'down', 'flat'].includes(quoteTone) ? quoteTone : 'flat';
    return `<a class="token-row ticker-row${rowClass}" href="${esc(href)}"${rowData} aria-label="${esc(ariaLabel)}">
      <span class="coin coin-${Number(coinTone) || 0}"><b>${esc(coinLabel)}</b><i></i></span>
      <span class="token-copy">
        <span class="ticker-line"><strong>${esc(headline)}</strong>${headlineMeta}<small class="ticker-age">${esc(age)}</small></span>
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
    const age = ago(row.entered_at || row.event_at);
    const events = Number(row.event_count) > 1
      ? `<span class="event-count">+${Number(row.event_count) - 1}</span>`
      : '';
    const rugValue = number(row.rug_score);
    const rugLevel = String(row.rug_level || 'unknown').toLowerCase();
    const tradeState = String(row.trade_state || '').toUpperCase();
    let safety = '';
    if (rugValue !== null && ['high', 'critical'].includes(rugLevel)) {
      safety = `<span class="rug-count rug-${esc(rugLevel)}">HIGH RISK</span>`;
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
    const label = `${row.ticker}, ${company}, ${money(row.price)}, ${percent(row.change_pct)}${marketLabel}${thesisLabel ? `, ${thesisLabel}` : ''}`;
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
    return renderShell({
      href: `/t/${encodeURIComponent(row.ticker)}`,
      ariaLabel: label,
      coinTone: row.coin_tone,
      coinLabel: row.coin_label || String(row.ticker).slice(0, 2),
      headline: row.ticker,
      headlineMeta: badge,
      age,
      company,
      detailMarkup: `${caseSource}${thesis}${caseSocial}${trackPrompt}`,
      catalyst: row.pulse_label || 'No recent event',
      catalystTone: catalystTone.trim(),
      catalystMarkup: `${events}${thesisCueMarkup}${safety}`,
      quoteValue: money(row.price),
      chartMarkup: `<svg class="mini-chart" data-ticker="${esc(row.ticker)}" viewBox="0 0 64 18" preserveAspectRatio="none" aria-hidden="true"><path class="chart-placeholder" d="M1 12 L13 10 L25 13 L39 8 L51 10 L63 7"/></svg>`,
      quoteMarkup: `<span class="quote-period">Today</span> ${esc(percent(row.change_pct))}`,
      quoteTone: changeClass,
      updated: Boolean(updateClass),
      dataTicker: row.ticker,
    });
  }

  function drawMiniChart(svg, points, annotations = []) {
    const rows = points
      .map(point => ({time: new Date(point.time).getTime(), price: number(point.price)}))
      .filter(point => point.price !== null)
      .slice(-36);
    if (rows.length < 2) {
      svg.classList.add('unavailable');
      return;
    }
    const values = rows.map(point => point.price);
    const low = Math.min(...values);
    const high = Math.max(...values);
    const spread = Math.max(high - low, Math.abs(high) * .002, .001);
    const path = values.map((value, index) => {
      const x = 1 + index / (values.length - 1) * 62;
      const y = 16 - (value - low) / spread * 14;
      return `${index ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(' ');
    const rising = values.at(-1) >= values[0];
    const entry = annotations.filter(item => item.type === 'pulse_entry').at(-1);
    const entryTime = entry ? new Date(entry.time).getTime() : NaN;
    let marker = '';
    if (Number.isFinite(entryTime) && Number.isFinite(rows[0].time) && entryTime >= rows[0].time) {
      let markerIndex = 0;
      rows.forEach((point, index) => {
        if (Math.abs(point.time - entryTime) < Math.abs(rows[markerIndex].time - entryTime)) markerIndex = index;
      });
      const x = 1 + markerIndex / (rows.length - 1) * 62;
      const y = 16 - (values[markerIndex] - low) / spread * 14;
      marker = `<line class="pulse-entry-line" x1="${x.toFixed(1)}" y1="1" x2="${x.toFixed(1)}" y2="17"/><circle class="pulse-entry-dot" cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="2.4"/>`;
    }
    svg.classList.toggle('rising', rising);
    svg.classList.toggle('falling', !rising);
    svg.innerHTML = `<path d="${path}" fill="none" stroke="currentColor" stroke-width="1.4" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>${marker}`;
    svg.classList.remove('unavailable');
    svg.classList.add('loaded');
  }

  function paintCharts(root = document) {
    root.querySelectorAll('.mini-chart').forEach(svg => {
      if (chartCache.has(svg.dataset.ticker)) {
        drawMiniChart(
          svg,
          chartCache.get(svg.dataset.ticker),
          annotationCache.get(svg.dataset.ticker) || [],
        );
      }
    });
  }

  async function loadCharts(url) {
    try {
      const response = await fetch(url);
      if (!response.ok) return;
      const data = await response.json();
      Object.entries(data.charts || {}).forEach(([ticker, points]) => chartCache.set(ticker, points));
      Object.entries(data.annotations || {}).forEach(([ticker, annotations]) => annotationCache.set(ticker, annotations));
      paintCharts();
    } catch (_) {}
  }

  window.TickerRow = Object.freeze({ago, loadCharts, paintCharts, render, renderShell});
})();
