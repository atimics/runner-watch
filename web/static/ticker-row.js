(() => {
  const chartCache = new Map();

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
    if (seconds < 5400) return Math.round(seconds / 60) + 'm';
    if (seconds < 129600) return Math.round(seconds / 3600) + 'h';
    return Math.round(seconds / 86400) + 'd';
  }

  function status(row) {
    if (row.has_update) return ['NEW', 'update'];
    const sessions = {regular: 'REG', pre: 'PRE', after: 'AH', overnight: 'OVN'};
    if (row.session && sessions[row.session]) return [sessions[row.session], 'session'];
    if (row.source === 'sec') return ['SEC', 'source'];
    if (row.source === 'quiet') return ['WATCH', 'quiet'];
    return ['', ''];
  }

  function fingerprint(row) {
    return [
      row.price,
      row.change_pct,
      row.score,
      row.event_count,
      row.pulse_label,
      row.event_at,
      row.session,
    ].join('|');
  }

  function render(row, options = {}) {
    const change = number(row.change_pct);
    const changeClass = change === null ? 'flat' : change >= 0 ? 'up' : 'down';
    const company = row.company || row.name || row.ticker;
    const [statusLabel, statusTone] = status(row);
    const badge = statusLabel
      ? `<small class="ticker-badge ticker-badge-${statusTone}">${esc(statusLabel)}</small>`
      : '';
    const age = ago(row.event_at);
    const events = Number(row.event_count) > 1
      ? `<span class="event-count">+${Number(row.event_count) - 1}</span>`
      : '';
    const gate = row.evidence_gate;
    const evidence = gate && Number(gate.count) > 0
      ? `<span class="evidence-count ${esc(gate.state)}">${Number(gate.count)} SIG</span>`
      : '';
    const catalystTone = row.sentiment === 'risk' ? ' risk' : row.sentiment === 'gap' ? ' gap' : '';
    const updated = options.updated ?? row.has_update;
    const label = `${row.ticker}, ${company}, ${money(row.price)}, ${percent(row.change_pct)}`;
    return `<a class="token-row ticker-row${updated ? ' is-updated' : ''}" href="/t/${encodeURIComponent(row.ticker)}" data-ticker-row="${esc(row.ticker)}" aria-label="${esc(label)}">
      <span class="coin coin-${Number(row.coin_tone) || 0}"><b>${esc(row.coin_label || String(row.ticker).slice(0, 2))}</b><i></i></span>
      <span class="token-copy">
        <span class="ticker-line"><strong>${esc(row.ticker)}</strong>${badge}<small class="ticker-age">${esc(age)}</small></span>
        <span class="company-name">${esc(company)}</span>
        <span class="catalyst${catalystTone}">${esc(row.pulse_label || 'Watching for changes')}${events}${evidence}</span>
      </span>
      <span class="quote">
        <strong>${esc(money(row.price))}</strong>
        <svg class="mini-chart" data-ticker="${esc(row.ticker)}" viewBox="0 0 64 18" preserveAspectRatio="none" aria-hidden="true"><path class="chart-placeholder" d="M1 12 L13 10 L25 13 L39 8 L51 10 L63 7"/></svg>
        <small class="${changeClass}">${esc(percent(row.change_pct))}</small>
      </span>
    </a>`;
  }

  function drawMiniChart(svg, points) {
    const values = points.map(point => number(point.price)).filter(value => value !== null).slice(-36);
    if (values.length < 2) {
      svg.classList.add('unavailable');
      return;
    }
    const low = Math.min(...values);
    const high = Math.max(...values);
    const spread = Math.max(high - low, Math.abs(high) * .002, .001);
    const path = values.map((value, index) => {
      const x = 1 + index / (values.length - 1) * 62;
      const y = 16 - (value - low) / spread * 14;
      return `${index ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`;
    }).join(' ');
    const rising = values.at(-1) >= values[0];
    svg.classList.toggle('rising', rising);
    svg.classList.toggle('falling', !rising);
    svg.innerHTML = `<path d="${path}" fill="none" stroke="currentColor" stroke-width="1.4" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>`;
    svg.classList.remove('unavailable');
    svg.classList.add('loaded');
  }

  function paintCharts(root = document) {
    root.querySelectorAll('.mini-chart').forEach(svg => {
      if (chartCache.has(svg.dataset.ticker)) {
        drawMiniChart(svg, chartCache.get(svg.dataset.ticker));
      }
    });
  }

  async function loadCharts(url) {
    try {
      const response = await fetch(url);
      if (!response.ok) return;
      const data = await response.json();
      Object.entries(data.charts || {}).forEach(([ticker, points]) => chartCache.set(ticker, points));
      paintCharts();
    } catch (_) {}
  }

  window.TickerRow = Object.freeze({ago, fingerprint, loadCharts, paintCharts, render});
})();
