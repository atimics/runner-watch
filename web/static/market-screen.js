(() => {
  'use strict';
  const node = document.getElementById('screenData');
  let screen = node ? JSON.parse(node.textContent) : null;
  // Late-loaded detail views consume the latest accepted response.
  if (node) node.ratiScreenDetail = screen;
  document.addEventListener('error', event => { if (event.target.matches?.('[data-portrait]')) event.target.hidden = true; }, true);
  const chart = document.querySelector('.price-chart');
  const status = document.querySelector('[data-chart-status]');
  let activeTag = null;
  function filterControls() {
    const filters = document.querySelector('[data-tag-filters]');
    if (!filters) return;
    filters.querySelectorAll('[data-retained-filter]').forEach(chip => {
      if (chip.dataset.tag !== activeTag) chip.remove();
    });
    filters.querySelectorAll('.chip').forEach(chip => {
      chip.setAttribute('aria-pressed', String((chip.dataset.tag || '') === (activeTag || '')));
    });
    filters.querySelector('.chip-all')?.toggleAttribute('hidden', !activeTag);
    if (!filters.querySelector('.chip:not(.chip-all)')) filters.remove();
  }
  function applyTagFilter() {
    const surface = document.querySelector('[data-live-surface]');
    if (!surface) return;
    const rows = [...surface.querySelectorAll('[data-tag-list] .ticker')];
    rows.forEach(row => {
      row.hidden = Boolean(activeTag) && (row.dataset.tag || '') !== activeTag;
    });
    const showEmpty = Boolean(activeTag) && !rows.some(row => !row.hidden);
    let empty = surface.querySelector('[data-filter-empty]');
    if (showEmpty && !empty) {
      empty = document.createElement('div');
      empty.className = 'empty'; empty.dataset.filterEmpty = '';
      empty.setAttribute('role', 'status');
      const message = document.createElement('p'), all = document.createElement('button');
      message.textContent = 'No results in this state.';
      all.type = 'button'; all.className = 'chip'; all.dataset.clearTagFilter = '';
      all.textContent = 'Show all'; empty.append(message, all); surface.append(empty);
    }
    if (empty) empty.hidden = !showEmpty;
    const serverEmpty = surface.querySelector('.empty:not([data-filter-empty])');
    if (serverEmpty) serverEmpty.hidden = showEmpty;
    filterControls();
  }
  function refreshTagFilters(next) {
    const current = document.querySelector('[data-tag-filters]');
    const focused = current?.contains(document.activeElement) ? document.activeElement.dataset.tag : null;
    let incoming = next.querySelector('[data-tag-filters]');
    if (activeTag && !incoming?.querySelector(`.chip[data-tag="${activeTag}"]`)) {
      if (!incoming) {
        incoming = document.createElement('div');
        incoming.className = 'tag-filters'; incoming.dataset.tagFilters = '';
        incoming.setAttribute('aria-label', 'Filter by state');
      }
      const retained = document.createElement('button');
      retained.type = 'button'; retained.className = 'chip chip-' + activeTag;
      retained.dataset.tag = activeTag; retained.dataset.retainedFilter = '';
      const dot = document.createElement('i');
      retained.append(dot, `0 ${activeTag.replaceAll('-', ' ')}`); incoming.prepend(retained);
      if (!incoming.querySelector('.chip-all')) {
        const all = document.createElement('button');
        all.type = 'button'; all.className = 'chip chip-all'; all.dataset.tag = '';
        all.textContent = 'All'; incoming.append(all);
      }
    }
    if (current) {
      if (incoming) current.replaceWith(incoming);
      else current.remove();
    } else if (incoming) {
      const search = document.querySelector('.topbar .search');
      if (search) search.before(incoming);
    }
    filterControls();
    if (focused !== null) {
      const target = [...document.querySelectorAll('[data-tag-filters] .chip')]
        .find(chip => chip.dataset.tag === focused && !chip.hidden);
      (target || document.querySelector('#marketSearch'))?.focus();
    }
  }
  document.addEventListener('click', event => {
    if (event.target.closest('[data-clear-tag-filter]')) {
      activeTag = null; applyTagFilter();
      (document.querySelector('[data-tag-filters] .chip') || document.querySelector('#marketSearch'))?.focus();
      return;
    }
    const chip = event.target.closest('[data-tag-filters] .chip');
    if (!chip) return;
    const tag = chip.dataset.tag || '';
    activeTag = (!tag || activeTag === tag) ? null : tag;
    applyTagFilter();
    if (!chip.isConnected || chip.hidden) {
      (document.querySelector('[data-tag-filters] .chip:not([hidden])') || document.querySelector('#marketSearch'))?.focus();
    }
  });
  applyTagFilter();
  const put = (selector, value) => { const el = document.querySelector(selector); if (el) el.textContent = value || ''; };
  let filingMarker = null, chartBounds = null;
  function markFiling() {
    if (!chart) return;
    chart.querySelector('.chart-filing-marker')?.remove();
    document.querySelector('.chart-filing-note')?.remove();
    if (!filingMarker?.time) return;
    const time = Date.parse(filingMarker.time);
    if (!Number.isFinite(time)) return;
    const note = document.createElement('p'); note.className = 'chart-filing-note';
    const within = chartBounds && time >= chartBounds[0] && time <= chartBounds[1];
    note.textContent = `${filingMarker.label || 'Selected filing'}${within ? ' · Marked on the chart' : ' · Price history for this date can be explored as more prices are saved.'}`;
    chart.after(note);
    if (within) {
      const line = document.createElementNS('http://www.w3.org/2000/svg','line');
      const x = chartBounds[0] === chartBounds[1] ? 400 : 8+(time-chartBounds[0])/(chartBounds[1]-chartBounds[0])*784;
      Object.entries({x1:x,x2:x,y1:0,y2:280,class:'chart-filing-marker'}).forEach(([key,value]) => line.setAttribute(key,value)); chart.append(line);
    }
  }
  document.addEventListener('rati:map-time',event => {filingMarker = event.detail; markFiling();});
  // The action tag over time, drawn onto the line. A reader can see where a
  // name turned from watch to setup to running without reading a table.
  let chartStates = [];
  let toneFilter = null;
  const STATE_TONES = ['running', 'setup', 'extended', 'avoid', 'watch', 'paused'];
  const STATE_LABELS = {running:'Running', setup:'Setup', extended:'Extended', avoid:'Avoid', watch:'Watch', paused:'Paused'};
  function renderStateLegend() {
    const legend = document.querySelector('[data-chart-state-legend]');
    if (!legend) return;
    const tones = new Set();
    chartStates.forEach(change => { const tone = String(change?.tone || ''); if (STATE_TONES.includes(tone)) tones.add(tone); });
    const ordered = STATE_TONES.filter(tone => tones.has(tone));
    legend.replaceChildren();
    legend.hidden = ordered.length === 0;
    ordered.forEach(tone => {
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'state-legend-chip tone-' + tone;
      button.textContent = STATE_LABELS[tone];
      button.setAttribute('aria-pressed', String(toneFilter === tone));
      button.addEventListener('click', () => {
        toneFilter = toneFilter === tone ? null : tone;
        [...legend.querySelectorAll('button')].forEach(control => control.setAttribute('aria-pressed', String(control === button && toneFilter === tone)));
        draw(screen?.series || [], screen?.gap);
      });
      legend.append(button);
    });
  }
  function toneAt(time) {
    let tone = '';
    for (const change of chartStates) {
      const at = Date.parse(change.time);
      if (!Number.isFinite(at) || at > time) break;
      tone = change.tone;
    }
    return tone;
  }
  function paintStates(data, coords) {
    if (!chart) return;
    chart.querySelectorAll('.chart-state').forEach(node => node.remove());
    if (!chartStates.length || data.length < 2) return;
    const base = chart.querySelector('.chart-line');
    const ns = 'http://www.w3.org/2000/svg';
    let run = [], runTone = toneAt(data[0][0]);
    const flush = () => {
      if (run.length > 1 && runTone) {
        const path = document.createElementNS(ns, 'path');
        path.setAttribute('class', 'chart-state state-' + runTone);
        path.setAttribute('d', run.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' '));
        if (toneFilter && toneFilter !== runTone) path.style.opacity = '0.15';
        base.after(path);
      }
      run = run.length ? [run[run.length - 1]] : [];
    };
    data.forEach((point, index) => {
      const tone = toneAt(point[0]);
      if (tone !== runTone) { flush(); runTone = tone; }
      run.push(coords[index]);
    });
    flush();
  }
  /* The dashed stretch between the last saved bar and the clock. It is drawn
     flat with a widening band and never pretends to be a candle: the label says
     what it is, including when the market is closed. */
  function drawGap(gap, projector) {
    chart.querySelectorAll('.chart-gap').forEach(node => node.remove());
    const tail = (gap?.path || [])
      .map(point => ({...point, t: Date.parse(point.time)}))
      .filter(point => Number.isFinite(point.t));
    if (!gap || tail.length < 1) return;
    const points = [{t: Date.parse(gap.anchor_time), price: gap.anchor_price, low: gap.anchor_price, high: gap.anchor_price}, ...tail]
      .filter(point => Number.isFinite(point.t));
    if (points.length < 2) return;
    const ns = 'http://www.w3.org/2000/svg';
    const [x, y] = projector;
    const upper = points.map(point => `${x(point.t).toFixed(2)},${y(point.high).toFixed(2)}`);
    const lower = [...points].reverse().map(point => `${x(point.t).toFixed(2)},${y(point.low).toFixed(2)}`);
    const band = document.createElementNS(ns, 'path');
    band.setAttribute('class', 'chart-gap chart-gap-band');
    band.setAttribute('d', `M${upper.join('L')}L${lower.join('L')}Z`);
    const line = document.createElementNS(ns, 'path');
    line.setAttribute('class', 'chart-gap chart-gap-line');
    line.setAttribute('d', points.map((point, index) => `${index ? 'L' : 'M'}${x(point.t).toFixed(2)},${y(point.price).toFixed(2)}`).join(''));
    const base = chart.querySelector('.chart-line');
    if (base) { base.before(band); base.after(line); } else { chart.append(band, line); }
  }
  function draw(points, gap) {
    if (!chart) return;
    const ordered = new Map();
    (points || []).forEach(p => { const t = Date.parse(p.time); if (Number.isFinite(t) && Number.isFinite(p.value) && p.value > 0) ordered.set(t,p.value); });
    const data = [...ordered].sort((a,b)=>a[0]-b[0]);
    const gapPoints = (gap?.path || []).map(point => ({...point, t: Date.parse(point.time)})).filter(point => Number.isFinite(point.t));
    const gapEnd = gapPoints.length ? gapPoints[gapPoints.length - 1].t : null;
    chartBounds = data.length ? [data[0][0], Math.max(data[data.length-1][0], gapEnd || 0)] : null;
    markFiling();
    const money = value => '$' + value.toLocaleString('en-US', value < 1 ? {maximumSignificantDigits: 6} : {minimumFractionDigits: 2, maximumFractionDigits: 6});
    const label = t => new Date(t).toLocaleString('en-US', {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'UTC'}) + ' UTC';
    const dot = chart.querySelector('.chart-point');
    if (!data.length) {
      status.hidden = false; status.textContent = 'Price history will appear here.';
      chart.setAttribute('hidden', ''); chart.setAttribute('aria-label', 'Price history is pending.');
      const legend = document.querySelector('[data-chart-state-legend]');
      if (legend) legend.hidden = true;
      return;
    }
    const lows = data.map(p=>p[1]), highs = data.map(p=>p[1]);
    gapPoints.forEach(point => { lows.push(point.low); highs.push(point.high); });
    const low = Math.min(...lows), high = Math.max(...highs);
    const start = data[0][0], end = Math.max(data[data.length-1][0], gapEnd || 0);
    const first = data[0][1], last = data[data.length-1][1];
    const projectX = value => end === start ? 400 : 8 + (value - start)/(end - start)*784;
    const projectY = value => high === low ? 140 : 260 - (value - low)/(high - low)*240;
    const move = ((last / first - 1) * 100).toLocaleString('en-US', {signDisplay:'always', maximumFractionDigits:6});
    const summary = data.length === 1 ? `One saved price: ${money(first)}` : `${money(first)} → ${money(last)} · ${move}%`;
    const liveNote = data.length ? `${label(start)}${data.length > 1 ? ' to ' + label(data[data.length-1][0]) : ''}.` : '';
    chart.setAttribute('aria-label', `Price history: ${summary}. ${liveNote}${gapPoints.length ? ' ' + (gap.label || 'Dashed line is our projection over the price gap.') : ''}`);
    const coords = data.map(p=>[projectX(p[0]), projectY(p[1])]);
    const line = coords.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');
    chart.querySelector('.chart-line').setAttribute('d',line);
    chart.querySelector('.chart-area').setAttribute('d',data.length === 1 ? '' : `${line} L792,280 L8,280 Z`);
    paintStates(data, coords);
    dot.toggleAttribute('hidden', data.length !== 1); dot.setAttribute('cx', '400'); dot.setAttribute('cy', '140');
    drawGap(gap, [projectX, projectY]);
    chart.removeAttribute('hidden'); status.hidden = true;
    renderStateLegend();
  }
  chartStates = screen?.states || [];
  draw(screen?.series, screen?.gap);
  const pageKey = () => location.pathname + location.search;
  let requestNumber = 0;
  let action = null;
  const actionKey = a => a ? a.endpoint + ':' + (a.body?.selection || '') : '';
  const sameSubject = next => next?.market === screen?.market && next?.item?.id === screen?.item?.id;
  function renderDetail(next) {
    if (next.market === 'sports' && typeof next.opinion_html === 'string') {
      const current = document.querySelector('.sports-opinion');
      const incoming = new DOMParser().parseFromString(next.opinion_html, 'text/html').querySelector('.sports-opinion');
      if (current && incoming && current.outerHTML !== incoming.outerHTML) {
        const openDetails = [...current.querySelectorAll('details[open]')].map(detail => detail.className);
        incoming.querySelectorAll('details').forEach(detail => { detail.open = openDetails.includes(detail.className); });
        if (!current.contains(document.activeElement)) current.replaceWith(incoming);
      }
    }
    put('[data-value]', next.item.value);
    put('[data-change]', next.item.change);
    put('[data-time]', next.item.time);
    const quoteScope = document.querySelector('.asset-quote .quote-scope');
    if (quoteScope && next.market === 'sports') quoteScope.hidden = next.item.time === next.item.change;
    const assessment = document.querySelector('[data-assessment-state]');
    if (assessment) {
      put('[data-assessment-tag]', next.item.tag);
      put('[data-assessment-reason]', next.item.eligibility_note);
      put('[data-assessment-quote]', next.item.assessment_quote_label);
      assessment.hidden = !next.item.tag;
      assessment.className = `state-chip chip-${next.item.tag_tone || 'none'}`;
      assessment.title = [next.item.eligibility_note, next.item.assessment_quote_label].filter(Boolean).join(' · ');
    }
    const move = document.querySelector('[data-change]');
    if (move) move.className = ['up','down','neutral'].includes(next.item.tone) ? next.item.tone : 'neutral';
    const record = document.querySelector('[data-call-record]');
    if (record) {
      record.hidden = !next.call || next.call.status === 'none';
      ['choice','entry','terms','outcome','reward'].forEach(key => put(`[data-call-${key}]`, next.call?.[key]));
    }
    // The close control lives with the Call it closes, so keep it in step there
    // instead of letting it reappear in the action row.
    const closing = (next.actions || []).findIndex(item => item.label === 'Close Call');
    let close = record?.querySelector('.call-close');
    if (record && closing >= 0) {
      if (!close) {
        close = document.createElement('button');
        close.type = 'button';
        close.className = 'call-close';
        (record.querySelector('.call-actions') || record).append(close);
      }
      close.textContent = next.actions[closing].label;
      close.dataset.action = String(closing);
    } else {
      close?.remove();
    }
    const facts = document.querySelector('[data-facts]');
    if (facts) {
      facts.replaceChildren(...next.facts.map(fact => {
        const row = document.createElement('div'), label = document.createElement('dt'), value = document.createElement('dd');
        label.textContent = fact.label; value.textContent = fact.value; row.append(label, value); return row;
      }));
      facts.hidden = next.facts.length === 0;
    }
    (next.teams || []).forEach((team, index) => {
      const el = document.querySelectorAll('.teams > div')[index];
      if (el) el.querySelector('strong').textContent = team.score;
    });
    put('[data-game-note]', next.note);
    const actions = document.querySelector('[data-screen-actions]');
    if (actions) {
      const shown = (next.actions || []).map((item, index) => ({item, index})).filter(entry => entry.index !== closing);
      shown.forEach((entry, position) => {
        let control = actions.children[position];
        if (!control) {
          control = document.createElement(actions.dataset.authenticated === 'true' ? 'button' : 'a');
          if (control.tagName === 'BUTTON') control.type = 'button';
          else { control.href = actions.dataset.loginUrl; control.className = 'primary'; }
          actions.append(control);
        }
        control.textContent = entry.item.label;
        if (control.tagName === 'BUTTON') control.dataset.action = String(entry.index);
      });
      while (actions.children.length > shown.length) {
        const last = actions.lastElementChild;
        if (last === document.activeElement) {
          const message = document.querySelector('[data-action-status]');
          message.tabIndex = -1; message.focus();
        }
        last.remove();
      }
    }
    screen = next;
    if (node) node.ratiScreenDetail = next;
    if (next.states) chartStates = next.states;
    draw(next.series, next.gap);
    node?.dispatchEvent(new CustomEvent('rati:screen-detail', {detail:next}));
  }
  async function refreshDetail() {
    if (!screen?.refresh_url) return null;
    const key = pageKey(), subject = screen.item.id, market = screen.market, version = ++requestNumber;
    try {
      const response = await fetch(screen.refresh_url, {cache:'no-store'});
      if (!response.ok || response.redirected) return null;
      const next = await response.json();
      if (pageKey() !== key || version !== requestNumber || screen.item.id !== subject || screen.market !== market || !sameSubject(next)) return null;
      renderDetail(next);
      return next;
    } catch (_) { return null; }
  }
  let surfaceRequestNumber = 0;
  async function refreshSurface() {
    if (document.hidden) return;
    if (screen?.refresh_url) { await refreshDetail(); return; }
    let surface = document.querySelector('[data-live-surface]');
    if (!surface || surface.contains(document.activeElement) || document.querySelector('dialog[open]')) return;
    const key = pageKey(), version = ++surfaceRequestNumber;
    try {
      const response = await fetch(screen?.surface_url || location.href);
      if (!response.ok || response.redirected) return;
      const html = await response.text();
      if (pageKey() !== key || version !== surfaceRequestNumber) return;
      const next = new DOMParser().parseFromString(html, 'text/html');
      const content = next.querySelector('[data-live-surface]');
      surface = document.querySelector('[data-live-surface]');
      if (!content || !surface || document.querySelector('dialog[open]') || surface.contains(document.activeElement)) return;
      if (content.innerHTML !== surface.innerHTML) {
        const opened = new Map([...surface.querySelectorAll('[data-connection][open]')].map(el => [el.dataset.connection, !!el.querySelector('.more-connections[open]')]));
        content.querySelectorAll('[data-connection]').forEach(el => { if (opened.has(el.dataset.connection)) { el.open = true; const more = el.querySelector('.more-connections'); if (more) more.open = opened.get(el.dataset.connection); } });
        surface.replaceWith(content);
      }
      refreshTagFilters(next);
      applyTagFilter();
      // Surface replacement contains fresh SVG shells. Repaint saved histories
      // immediately, then refresh the same bounded batch (not one call per row).
      const charted = content.querySelector('.market-stocks, .market-memecoins');
      if (charted) {
        window.TickerRow?.paintCharts();
        window.TickerRow?.loadCharts(charted.classList.contains('market-memecoins') ? '/api/memecoins/charts' : '/api/pulse/charts');
      }
    } catch (_) { /* Keep the saved view during connection recovery. */ }
  }
  if (screen?.refresh_url) refreshDetail();
  else if (screen?.chart_url) fetch(screen.chart_url).then(r=>r.json()).then(p=>{if(p.states)chartStates=p.states;draw(p.points, p.gap);}).catch(()=>draw(screen.series, screen.gap));
  setInterval(refreshSurface, 60000);
  function showTerms(selected) {
    put('[data-confirm-title]', selected.label);
    const price = selected.body?.expected_price;
    put('[data-confirm-terms]', selected.preview || (price ? `${selected.label} at $${price}. This records a paper Call on your public profile.` : 'This records a paper Call on your public profile.'));
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    const dialog = document.querySelector('.call-confirm');
    if (button.matches('[data-action]')) {
      const selected = screen.actions[Number(button.dataset.action)], key = pageKey();
      if (!selected) return;
      if (screen.refresh_url) {
        const next = await refreshDetail();
        if (pageKey() !== key) return;
        action = next?.actions.find(item => actionKey(item) === actionKey(selected));
        if (!action) { put('[data-action-status]', 'Please review the current Call state and try again.'); return; }
      } else action = selected;
      showTerms(action); put('[data-confirm-status]', ''); dialog.showModal(); return;
    }
    if (button.matches('[data-cancel]')) { action = null; dialog.close(); return; }
    if (!button.matches('[data-confirm]') || !action) return;
    button.disabled = true;
    const chosen = action, key = pageKey();
    try {
      if (screen.refresh_url) {
        const next = await refreshDetail();
        if (!dialog.open || pageKey() !== key || action !== chosen) return;
        if (!next) { put('[data-confirm-status]', 'Please try again to check the current terms.'); return; }
        const current = next.actions.find(a => actionKey(a) === actionKey(chosen));
        if (!current) { put('[data-confirm-status]', 'This Call has changed. Close this window to see its current state.'); return; }
        if (JSON.stringify(current.body) !== JSON.stringify(chosen.body)) {
          action = current; showTerms(current);
          put('[data-confirm-status]', 'The terms changed. Review them and confirm again.'); return;
        }
      }
      if (pageKey() !== key || !dialog.open || action !== chosen) return;
      const r = await fetch(chosen.endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(chosen.body)});
      if (pageKey() !== key) return;
      if (!r.ok) { put('[data-action-status]',r.status===401 ? 'Log in to make a Call.' : 'Please try again when the price is current.'); dialog.close(); await refreshDetail(); return; }
      location.reload();
    } catch (_) { put('[data-action-status]','Please try again in a moment.'); dialog.close(); }
    finally { button.disabled = false; }
  });
})();

(() => {
  'use strict';
  const input = document.getElementById('marketSearch');
  if (!input) return;
  const form = input.closest('form'), market = form.dataset.market;
  const key = `rati:recently-viewed:${market}:v1`;
  const popup = document.createElement('div');
  popup.className = 'search-popup'; popup.hidden = true;
  const heading = document.createElement('div'); heading.className = 'search-popup-heading';
  const label = document.createElement('span');
  const clear = document.createElement('button'); clear.type = 'button'; clear.textContent = 'Clear';
  clear.setAttribute('aria-label', 'Clear recently viewed'); heading.append(label, clear);
  const list = document.createElement('div'); list.id = 'marketSearchResults'; list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', 'Search suggestions');
  const status = document.createElement('p'); status.setAttribute('role', 'status');
  popup.append(heading, list, status); form.append(popup);
  input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list');
  input.setAttribute('aria-controls', list.id); input.setAttribute('aria-expanded', 'false');
  let recent = [], active = -1, timer, controller, version = 0;
  function safe(item) {
    if (!item || typeof item.name !== 'string' || typeof item.href !== 'string') return null;
    try {
      const url = new URL(item.href, location.origin);
      const prefix = market === 'stocks' ? '/stock/' : market === 'memecoins' ? '/memecoins/coin/' : '/game/';
      if (url.origin !== location.origin || !url.pathname.startsWith(prefix)) return null;
      const selection = new URLSearchParams();
      if (market === 'sports') {
        for (const key of ['contract', 'outcome']) {
          const value = url.searchParams.get(key);
          if (value && /^[a-z0-9-]{1,64}$/.test(value)) selection.set(key, value);
        }
      }
      const href = url.pathname + (selection.size ? `?${selection}` : '');
      return {name:item.name.slice(0,100), subtitle:String(item.subtitle || '').slice(0,160), href};
    } catch (_) { return null; }
  }
  try { const saved = JSON.parse(localStorage.getItem(key) || '[]'); if (Array.isArray(saved)) recent = saved.map(safe).filter(Boolean).slice(0,10); } catch (_) { /* Keep this visit usable when storage is unavailable. */ }
  function save() { try { localStorage.setItem(key, JSON.stringify(recent)); } catch (_) { /* Keep history for this page session. */ } }
  const data = document.getElementById('screenData');
  if (data) {
    try {
      const screen = data.ratiScreenDetail || JSON.parse(data.textContent);
      const item = screen.kind === 'detail' && screen.market === market ? safe(screen.item) : null;
      if (item) { recent = [item, ...recent.filter(row => row.href !== item.href)].slice(0,10); save(); }
    } catch (_) { /* Render search independently of detail data. */ }
  }
  function select(index) {
    const options = [...list.children]; active = index;
    options.forEach((option, i) => option.setAttribute('aria-selected', String(i === index)));
    if (options[index]) { input.setAttribute('aria-activedescendant', options[index].id); options[index].scrollIntoView({block:'nearest'}); }
    else input.removeAttribute('aria-activedescendant');
  }
  function close() {
    clearTimeout(timer); controller?.abort(); version++;
    popup.hidden = true; input.setAttribute('aria-expanded', 'false'); select(-1);
  }
  function render(rows, title, message = '') {
    list.replaceChildren(); select(-1); label.textContent = title;
    clear.hidden = title !== 'Recently viewed' || !recent.length;
    status.textContent = message; status.hidden = !message;
    rows.forEach((row, i) => {
      const link = document.createElement('a'); link.href = row.href;
      link.id = `marketSearchOption${i}`; link.setAttribute('role', 'option'); link.setAttribute('aria-selected', 'false'); link.tabIndex = -1;
      const name = document.createElement('strong'), subtitle = document.createElement('small');
      name.textContent = row.name; subtitle.textContent = row.subtitle; link.append(name, subtitle); list.append(link);
    });
    popup.hidden = false; input.setAttribute('aria-expanded', 'true');
  }
  function update() {
    clearTimeout(timer); controller?.abort(); const current = ++version;
    const query = input.value.trim();
    if (!query) { render(recent, 'Recently viewed', recent.length ? '' : 'Items you view will appear here.'); return; }
    render([], 'Matches', 'Searching…');
    timer = setTimeout(async () => {
      controller = new AbortController();
      try {
        const url = new URL(form.action); url.searchParams.set('q', query); url.searchParams.set('view', 'list');
        const response = await fetch(url, {signal:controller.signal});
        if (!response.ok || response.redirected) throw new Error('Search unavailable');
        const html = await response.text();
        if (current !== version) return;
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const rows = [...doc.querySelectorAll('.ticker-list a.ticker')].map(link => safe({href:link.getAttribute('href'),name:link.querySelector('.ticker-name strong')?.textContent,subtitle:link.querySelector('.ticker-name small')?.textContent})).filter(Boolean);
        render(rows.slice(0,8), 'Matches', rows.length ? '' : 'Try another name or symbol.');
      } catch (error) {
        if (current === version && error.name !== 'AbortError') render([], 'Matches', 'Press Enter to search.');
      }
    }, 200);
  }
  input.addEventListener('focus', update);
  input.addEventListener('click', () => { if (popup.hidden) update(); });
  input.addEventListener('input', update);
  input.addEventListener('keydown', event => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); if (popup.hidden) update();
      const count = list.children.length;
      if (count) select((active + (event.key === 'ArrowDown' ? 1 : active < 0 ? 0 : -1) + count) % count);
    }
    if (event.key === 'Enter' && !popup.hidden && active >= 0) { event.preventDefault(); list.children[active].click(); }
  });
  clear.addEventListener('click', () => { recent = []; save(); input.value = ''; input.focus(); update(); });
  document.addEventListener('pointerdown', event => { if (!form.contains(event.target)) close(); });
  form.addEventListener('focusout', event => { if (!form.contains(event.relatedTarget)) close(); });
  form.addEventListener('submit', close);
})();
