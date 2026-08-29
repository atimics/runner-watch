(() => {
  const POLL_INTERVAL = 30000;

  function escapeHtml(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#039;');
  }

  function safeToken(value, fallback = '') {
    const token = String(value ?? '').toLowerCase().replace(/[^a-z0-9_-]/g, '-');
    return token || fallback;
  }

  function number(value, digits = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed.toFixed(digits) : (0).toFixed(digits);
  }

  function signed(value, digits = 1) {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return '—';
    return `${parsed >= 0 ? '+' : ''}${parsed.toFixed(digits)}`;
  }

  function localTime(value, options = {}) {
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return value ? String(value) : 'pending';
    return date.toLocaleString([], {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      timeZoneName: 'short',
      ...options,
    });
  }

  function shortGameTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return value ? String(value) : 'pending';
    return date.toLocaleString([], {
      weekday: 'short',
      hour: 'numeric',
      minute: '2-digit',
    });
  }

  function formatLocalTimes(root = document) {
    root.querySelectorAll('[data-local-time]').forEach(node => {
      node.textContent = localTime(node.dataset.localTime);
    });
  }

  function historySvg(history, className = '') {
    if (!history) return '';
    const points = escapeHtml(history.plot_points || '');
    const dotX = escapeHtml(history.dot_x ?? 46);
    const dotY = escapeHtml(history.dot_y ?? 12);
    return `<svg class="edge-spark ${className}" viewBox="0 0 92 24" role="img" aria-label="${escapeHtml(history.label || 'Edge history')}" preserveAspectRatio="none">
      <line class="edge-zero" x1="2" y1="12" x2="90" y2="12"></line>
      ${points ? `<polyline points="${points}"></polyline>` : ''}
      <circle cx="${dotX}" cy="${dotY}" r="2"></circle>
    </svg>`;
  }

  function pulseHistorySvg(history) {
    if (!history) {
      return '<svg class="mini-chart unavailable" viewBox="0 0 64 18" preserveAspectRatio="none" aria-hidden="true"><path class="chart-placeholder" d="M1 12 L13 10 L25 13 L39 8 L51 10 L63 7"></path></svg>';
    }
    const points = escapeHtml(history.plot_points || '');
    const dotX = escapeHtml(history.dot_x ?? 90);
    const dotY = escapeHtml(history.dot_y ?? 12);
    return `<svg class="mini-chart loaded rising" viewBox="0 0 92 24" preserveAspectRatio="none" aria-hidden="true">
      ${points ? `<polyline class="sports-history-line" points="${points}"></polyline>` : ''}
      <circle class="sports-history-dot" cx="${dotX}" cy="${dotY}" r="2.4"></circle>
    </svg>`;
  }

  function gameHref(prefix, id) {
    return `${prefix || ''}/game/${encodeURIComponent(String(id || ''))}`;
  }

  function pulseCard(event, prefix) {
    const prediction = event.prediction || {};
    const signal = event.signal_abbreviation || '';
    const model = event.model_winner_abbreviation || '';
    const opponentAbbreviation = event.model_winner_opponent_abbreviation
      || (event.model_winner_side === 'home' ? event.away_abbreviation : event.home_abbreviation)
      || '';
    const opponentName = event.model_winner_opponent_team_name
      || (event.model_winner_side === 'home' ? event.away_team_name : event.home_team_name)
      || '';
    const winProbability = Number(event.model_winner_probability_pct);
    const slightEdge = Number.isFinite(winProbability) && winProbability < 55;
    const projectionLabel = event.model_winner_label || (slightEdge ? 'SLIGHT EDGE' : 'PROJECTED');
    const ariaAction = event.model_winner_aria_action
      || (slightEdge ? 'has a slight model edge over' : 'is projected to beat');
    const label = `${event.model_winner_team_name || model} ${ariaAction} ${opponentName} with a ${number(event.model_winner_probability_pct)} percent win chance; value side ${signal} with a ${signed(prediction.edge_pct)} percentage-point model edge`;
    return TickerRow.renderShell({
      href: gameHref(prefix, event.id),
      ariaLabel: label,
      coinTone: event.model_winner_coin_tone,
      coinLabel: model,
      headline: model,
      headlineMeta: `<small class="ticker-badge ticker-badge-update">${escapeHtml(projectionLabel)}</small>`,
      age: shortGameTime(event.start_time),
      company: event.model_winner_team_name || model,
      catalyst: `vs ${opponentAbbreviation} · ${String(event.league || '').toUpperCase()}`,
      catalystTone: 'gap',
      quoteValue: `${number(event.model_winner_probability_pct)}%`,
      chartMarkup: pulseHistorySvg(event.edge_history),
      quoteMarkup: `<span class="quote-period">Value</span> ${escapeHtml(signal)} ${signed(prediction.edge_pct)}pp`,
      quoteTone: 'up',
      dataSportsGame: event.id,
    });
  }

  function renderPulseEvents(events, prefix) {
    if (!events.length) {
      return '<div class="sports-empty"><strong>No matchups right now.</strong><p>Pulse stays quiet until a game clears the model-versus-market threshold.</p></div>';
    }
    return events.map(event => {
      const related = event.series_more || [];
      const cluster = related.length ? `<details class="series-cluster">
        <summary><span>${escapeHtml(event.away_abbreviation)}–${escapeHtml(event.home_abbreviation)} series</span><b>+${related.length} more game${related.length === 1 ? '' : 's'}</b></summary>
        <div class="series-games">${related.map(item => pulseCard(item, prefix)).join('')}</div>
      </details>` : '';
      return `${pulseCard(event, prefix)}${cluster}`;
    }).join('');
  }

  function radarCard(event, prefix, compact = false) {
    const live = event.radar_kind === 'live';
    const value = live
      ? `<b>${number(event.away_score)}–${number(event.home_score)}</b><small>${escapeHtml(event.status_detail)}</small>`
      : `<b class="${Number(event.radar_value) >= 0 ? 'positive' : 'negative'}">${signed(event.radar_value)}pp</b><small>${escapeHtml(String(event.radar_label || '').toLowerCase())}</small>`;
    return `<a class="radar-card ${safeToken(event.radar_kind)}${compact ? ' radar-card-compact' : ''}" href="${gameHref(prefix, event.id)}">
      <span class="radar-mark"><b>${escapeHtml(String(event.signal_abbreviation || '').slice(0, 3))}</b><small>${escapeHtml(String(event.league || '').toUpperCase())}</small></span>
      <span class="radar-copy"><span><strong>${escapeHtml(event.away_abbreviation)}</strong> at ${escapeHtml(event.home_abbreviation)} <b class="radar-kind">${escapeHtml(event.radar_label)}</b></span><small>${escapeHtml(event.radar_detail)}</small><time data-local-time="${escapeHtml(event.start_time)}">${escapeHtml(localTime(event.start_time))}</time></span>
      ${historySvg(event.edge_history, 'radar-spark')}
      <span class="radar-value">${value}</span>
      <span class="game-chevron" aria-hidden="true">›</span>
    </a>`;
  }

  function renderRadarEvents(events, prefix) {
    return events.length
      ? events.map(event => {
        const related = event.series_more || [];
        const cluster = related.length ? `<details class="series-cluster radar-series-cluster">
          <summary><span>${escapeHtml(event.away_abbreviation)}–${escapeHtml(event.home_abbreviation)} series</span><b>+${related.length} related game${related.length === 1 ? '' : 's'}</b></summary>
          <div class="series-games">${related.map(item => radarCard(item, prefix, true)).join('')}</div>
        </details>` : '';
        return `${radarCard(event, prefix)}${cluster}`;
      }).join('')
      : `<div class="sports-empty radar-empty-state"><span class="empty-radar" aria-hidden="true"></span><strong>No material change yet.</strong><p>Radar will light up when a Pulse signal moves by at least half a point or goes live.</p><a class="sports-empty-action" href="${prefix || ''}/">Back to Pulse</a></div>`;
  }

  function eventKey(event) {
    return String(event?.id || '');
  }

  function payloadChanged(current, next) {
    return JSON.stringify(current?.events || []) !== JSON.stringify(next?.events || []);
  }

  function restorePanelSelection(list) {
    const frame = document.querySelector('[data-desktop-frame]');
    const source = frame?.getAttribute('src');
    if (!source) return;
    const selected = new URL(source, window.location.href);
    list.querySelectorAll('a[href]').forEach(link => {
      const candidate = new URL(link.href, window.location.href);
      link.classList.toggle('desktop-panel-selected', candidate.pathname === selected.pathname);
    });
  }

  function queryEndpoint(path) {
    const endpoint = new URL(path, window.location.origin);
    const current = new URL(window.location.href);
    for (const name of ['league', 'view']) {
      if (current.searchParams.has(name)) endpoint.searchParams.set(name, current.searchParams.get(name));
    }
    return `${endpoint.pathname}${endpoint.search}`;
  }

  function mountPulse({initial, prefix = ''}) {
    let current = initial;
    let pending = null;
    let polling = false;
    const list = document.getElementById('sportsPulseList');
    const refresh = document.getElementById('sportsPulseRefresh');
    const count = document.getElementById('sportsPulseCount');
    const maturity = document.getElementById('sportsPulseMaturity');
    const status = document.getElementById('sportsPulseStatus');
    const updated = document.getElementById('sportsPulseUpdated');
    if (!list || !refresh) return null;

    function render() {
      list.innerHTML = renderPulseEvents(current.events || [], prefix);
      count.textContent = current.display_count ?? (current.events || []).length;
      if (maturity && current.model_record) {
        maturity.textContent = `Baseline ${current.model_record.games}/${current.model_record.sample?.target ?? '—'} graded`;
      }
      if (updated) {
        updated.dataset.localTime = current.updated_at || '';
        updated.textContent = localTime(current.updated_at);
      }
      restorePanelSelection(list);
      list.dispatchEvent(new Event('desktop-rows-rendered'));
    }

    refresh.addEventListener('click', () => {
      if (!pending) return;
      current = pending;
      pending = null;
      refresh.hidden = true;
      render();
    });

    async function poll() {
      if (polling || document.hidden) return;
      polling = true;
      try {
        const response = await fetch(queryEndpoint('/api/sports/pulse'));
        if (!response.ok) throw new Error('Sports Pulse refresh failed');
        const next = await response.json();
        status.textContent = 'Live';
        if (!payloadChanged(current, next)) {
          current = {...current, ...next};
          pending = null;
          refresh.hidden = true;
          return;
        }
        const currentIds = new Set((current.events || []).map(eventKey));
        const newCount = (next.events || []).filter(event => !currentIds.has(eventKey(event))).length;
        pending = next;
        refresh.textContent = newCount === 1 ? '1 new matchup' : (newCount > 1 ? `${newCount} new matchups` : 'Slate updated');
        refresh.hidden = false;
      } catch (_) {
        status.textContent = 'Offline';
      } finally {
        polling = false;
      }
    }

    render();
    formatLocalTimes();
    const timer = setInterval(poll, POLL_INTERVAL);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    return {poll, applyPending: () => refresh.click(), stop: () => clearInterval(timer)};
  }

  function mountRadar({initial, prefix = ''}) {
    let current = initial;
    let pending = null;
    let polling = false;
    const list = document.getElementById('sportsRadarList');
    const refresh = document.getElementById('sportsRadarRefresh');
    const count = document.getElementById('sportsRadarCount');
    const status = document.getElementById('sportsRadarStatus');
    if (!list || !refresh) return null;

    function render() {
      list.innerHTML = renderRadarEvents(current.events || [], prefix);
      count.textContent = current.display_count ?? (current.events || []).length;
      restorePanelSelection(list);
    }

    refresh.addEventListener('click', () => {
      if (!pending) return;
      current = pending;
      pending = null;
      refresh.hidden = true;
      render();
    });

    async function poll() {
      if (polling || document.hidden) return;
      polling = true;
      try {
        const response = await fetch(queryEndpoint('/api/sports/radar'));
        if (!response.ok) throw new Error('Sports Radar refresh failed');
        const next = await response.json();
        status.textContent = 'Live';
        if (!payloadChanged(current, next)) {
          pending = null;
          refresh.hidden = true;
          return;
        }
        const currentIds = new Set((current.events || []).map(eventKey));
        const newCount = (next.events || []).filter(event => !currentIds.has(eventKey(event))).length;
        pending = next;
        refresh.textContent = newCount === 1 ? '1 new event' : (newCount > 1 ? `${newCount} new events` : 'Radar updated');
        refresh.hidden = false;
      } catch (_) {
        status.textContent = 'Offline';
      } finally {
        polling = false;
      }
    }

    formatLocalTimes();
    const timer = setInterval(poll, POLL_INTERVAL);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
    return {poll, applyPending: () => refresh.click(), stop: () => clearInterval(timer)};
  }

  window.SportsLive = {formatLocalTimes, mountPulse, mountRadar};
})();
