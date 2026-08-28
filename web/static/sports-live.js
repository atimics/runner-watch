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

  function gameHref(prefix, id) {
    return `${prefix || ''}/game/${encodeURIComponent(String(id || ''))}`;
  }

  function pulseCard(event, prefix, compact = false) {
    const prediction = event.prediction || {};
    const signal = event.signal_abbreviation || '';
    const model = event.model_winner_abbreviation || '';
    const baselineRelationship = model === signal ? 'agrees' : 'favors opponent';
    const hasValueProbabilities = Number.isFinite(Number(event.model_probability_pct))
      && Number.isFinite(Number(event.market_probability_pct));
    const valueComparison = hasValueProbabilities
      ? `Model ${number(event.model_probability_pct)}% · Market ${number(event.market_probability_pct)}%`
      : 'Model and market building';
    const divergence = event.bovada_divergence_material
      ? `<span class="bovada-outlier">Bovada differs by ${number(event.bovada_divergence_pct, 1)} points</span>`
      : '';
    const label = `${event.away_team_name || ''} at ${event.home_team_name || ''}; baseline winner ${model} at ${number(event.model_winner_probability_pct)} percent; value side ${signal} with a ${signed(prediction.edge_pct)} percentage-point model edge`;
    return `<a class="game-card winner-card${compact ? ' winner-card-compact' : ''}" href="${gameHref(prefix, event.id)}" aria-label="${escapeHtml(label)}">
      <span class="matchup-copy">
        <span class="matchup-team${signal === event.away_abbreviation ? ' is-value' : ''}"><strong>${escapeHtml(event.away_abbreviation)}</strong><span>${escapeHtml(event.away_team_name)}</span>${signal === event.away_abbreviation ? '<small class="matchup-value">VALUE</small>' : ''}</span>
        <span class="matchup-team${signal === event.home_abbreviation ? ' is-value' : ''}"><strong>${escapeHtml(event.home_abbreviation)}</strong><span>${escapeHtml(event.home_team_name)}</span>${signal === event.home_abbreviation ? '<small class="matchup-value">VALUE</small>' : ''}</span>
        <span class="winner-context">${escapeHtml(String(event.league || '').toUpperCase())} · <time data-local-time="${escapeHtml(event.start_time)}">${escapeHtml(localTime(event.start_time))}</time></span>
      </span>
      <span class="winner-quote">
        <small>VALUE EDGE</small><strong>${signed(prediction.edge_pct)} pp</strong>
        <span class="winner-edge">${valueComparison}</span>
        <em class="baseline-winner">Baseline ${baselineRelationship} · ${number(event.model_winner_probability_pct)}%</em>
        ${divergence}
        ${historySvg(event.edge_history)}
      </span>
      <span class="game-chevron" aria-hidden="true"><small>View</small>›</span>
    </a>`;
  }

  function renderPulseEvents(events, prefix) {
    if (!events.length) {
      return '<div class="sports-empty"><strong>No Pulse signals right now.</strong><p>Pulse stays quiet until a game clears the model-versus-market threshold.</p></div>';
    }
    return events.map(event => {
      const related = event.series_more || [];
      const cluster = related.length ? `<details class="series-cluster">
        <summary><span>${escapeHtml(event.away_abbreviation)}–${escapeHtml(event.home_abbreviation)} series</span><b>+${related.length} more game${related.length === 1 ? '' : 's'}</b></summary>
        <div class="series-games">${related.map(item => pulseCard(item, prefix, true)).join('')}</div>
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
        maturity.textContent = `${current.model_record.games} of ${current.model_record.sample?.target ?? '—'} baseline results graded`;
      }
      if (updated) {
        updated.dataset.localTime = current.updated_at || '';
        updated.textContent = localTime(current.updated_at);
      }
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
        const response = await fetch(queryEndpoint('/api/sports/pulse'));
        if (!response.ok) throw new Error('Sports Pulse refresh failed');
        const next = await response.json();
        status.textContent = 'Live updates';
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
