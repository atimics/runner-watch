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
        draw(screen?.series || []);
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
  function draw(points) {
    if (!chart) return;
    const ordered = new Map();
    (points || []).forEach(p => { const t = Date.parse(p.time); if (Number.isFinite(t) && Number.isFinite(p.value) && p.value > 0) ordered.set(t,p.value); });
    const data = [...ordered].sort((a,b)=>a[0]-b[0]);
    chartBounds = data.length ? [data[0][0],data[data.length-1][0]] : null;
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
    const low = Math.min(...data.map(p=>p[1])), high = Math.max(...data.map(p=>p[1]));
    const start = data[0][0], end = data[data.length-1][0];
    const first = data[0][1], last = data[data.length-1][1];
    const move = ((last / first - 1) * 100).toLocaleString('en-US', {signDisplay:'always', maximumFractionDigits:6});
    const summary = data.length === 1 ? `One saved price: ${money(first)}` : `${money(first)} → ${money(last)} · ${move}%`;
    chart.setAttribute('aria-label', `Price history: ${summary}. ${label(start)}${data.length > 1 ? ' to ' + label(end) : ''}.`);
    const coords = data.map(p=>[end===start ? 400 : 8 + (p[0]-start)/(end-start)*784, high===low ? 140 : 260-(p[1]-low)/(high-low)*240]);
    const line = coords.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');
    chart.querySelector('.chart-line').setAttribute('d',line);
    chart.querySelector('.chart-area').setAttribute('d',data.length === 1 ? '' : `${line} L792,280 L8,280 Z`);
    paintStates(data, coords);
    dot.toggleAttribute('hidden', data.length !== 1); dot.setAttribute('cx', '400'); dot.setAttribute('cy', '140');
    chart.removeAttribute('hidden'); status.hidden = true;
    renderStateLegend();
  }
  chartStates = screen?.states || [];
  draw(screen?.series);
  const pageKey = () => location.pathname + location.search;
  let requestNumber = 0;
  let action = null;
  const actionKey = a => a ? a.endpoint + ':' + (a.body?.selection || '') : '';
  const sameSubject = next => next?.market === screen?.market && next?.item?.id === screen?.item?.id;
  function renderDetail(next) {
    put('[data-value]', next.item.value);
    put('[data-change]', next.item.change);
    put('[data-time]', next.item.time);
    const move = document.querySelector('[data-change]');
    if (move) move.className = ['up','down','neutral'].includes(next.item.tone) ? next.item.tone : 'neutral';
    const record = document.querySelector('[data-call-record]');
    if (record) {
      record.hidden = !next.call || next.call.status === 'none';
      ['choice','entry','terms','outcome','reward'].forEach(key => put(`[data-call-${key}]`, next.call?.[key]));
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
      next.actions.forEach((item, index) => {
        let control = actions.children[index];
        if (!control) {
          control = document.createElement(actions.dataset.authenticated === 'true' ? 'button' : 'a');
          if (control.tagName === 'BUTTON') control.type = 'button';
          else { control.href = actions.dataset.loginUrl; control.className = 'primary'; }
          actions.append(control);
        }
        control.textContent = item.label;
        if (control.tagName === 'BUTTON') control.dataset.action = String(index);
      });
      while (actions.children.length > next.actions.length) {
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
    draw(next.series);
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
      const response = await fetch(location.href);
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
    } catch (_) { /* Keep the saved view during connection recovery. */ }
  }
  if (screen?.refresh_url) refreshDetail();
  else if (screen?.chart_url) fetch(screen.chart_url).then(r=>r.json()).then(p=>{if(p.states)chartStates=p.states;draw(p.points);}).catch(()=>draw(screen.series));
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
