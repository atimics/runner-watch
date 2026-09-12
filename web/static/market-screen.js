(() => {
  'use strict';
  const node = document.getElementById('screenData');
  let screen = node ? JSON.parse(node.textContent) : null;
  document.addEventListener('error', event => { if (event.target.matches?.('[data-portrait]')) event.target.hidden = true; }, true);
  const chart = document.querySelector('.price-chart');
  const status = document.querySelector('[data-chart-status]');
  const put = (selector, value) => { const el = document.querySelector(selector); if (el) el.textContent = value || ''; };
  function draw(points) {
    if (!chart) return;
    const ordered = new Map();
    (points || []).forEach(p => { const t = Date.parse(p.time); if (Number.isFinite(t) && Number.isFinite(p.value) && p.value > 0) ordered.set(t,p.value); });
    const data = [...ordered].sort((a,b)=>a[0]-b[0]);
    const money = value => '$' + value.toLocaleString('en-US', value < 1 ? {maximumSignificantDigits: 6} : {minimumFractionDigits: 2, maximumFractionDigits: 6});
    const label = t => new Date(t).toLocaleString('en-US', {year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'UTC'}) + ' UTC';
    const dot = chart.querySelector('.chart-point');
    put('[data-chart-start]', ''); put('[data-chart-end]', ''); put('[data-chart-summary]', '');
    if (!data.length) {
      status.hidden = false; status.textContent = 'Price history will appear here.';
      chart.setAttribute('hidden', ''); chart.setAttribute('aria-label', 'Price history is pending.'); return;
    }
    const low = Math.min(...data.map(p=>p[1])), high = Math.max(...data.map(p=>p[1]));
    const start = data[0][0], end = data[data.length-1][0];
    const first = data[0][1], last = data[data.length-1][1];
    const move = ((last / first - 1) * 100).toLocaleString('en-US', {signDisplay:'always', maximumFractionDigits:6});
    const summary = data.length === 1 ? `One saved price: ${money(first)}` : `${money(first)} → ${money(last)} · ${move}% over this period`;
    put('[data-chart-summary]', summary);
    put('[data-chart-start]', label(start));
    if (data.length > 1) put('[data-chart-end]', label(end));
    chart.setAttribute('aria-label', `Price history: ${summary}. ${label(start)}${data.length > 1 ? ' to ' + label(end) : ''}.`);
    const coords = data.map(p=>[end===start ? 400 : 8 + (p[0]-start)/(end-start)*784, high===low ? 140 : 260-(p[1]-low)/(high-low)*240]);
    const line = coords.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');
    chart.querySelector('.chart-line').setAttribute('d',line);
    chart.querySelector('.chart-area').setAttribute('d',data.length === 1 ? '' : `${line} L792,280 L8,280 Z`);
    dot.toggleAttribute('hidden', data.length !== 1); dot.setAttribute('cx', '400'); dot.setAttribute('cy', '140');
    chart.removeAttribute('hidden'); status.hidden = true;
  }
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
    draw(next.series);
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
  async function refreshSurface() {
    if (document.hidden) return;
    if (screen?.refresh_url) { await refreshDetail(); return; }
    const surface = document.querySelector('[data-live-surface]');
    if (!surface || surface.contains(document.activeElement) || document.querySelector('dialog[open]')) return;
    const key = pageKey();
    try {
      const response = await fetch(location.href);
      if (!response.ok || response.redirected || pageKey() !== key) return;
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      const content = next.querySelector('[data-live-surface]');
      if (!content || document.querySelector('dialog[open]') || surface.contains(document.activeElement)) return;
      if (content.innerHTML !== surface.innerHTML) {
        const opened = new Map([...surface.querySelectorAll('[data-connection][open]')].map(el => [el.dataset.connection, !!el.querySelector('.more-connections[open]')]));
        content.querySelectorAll('[data-connection]').forEach(el => { if (opened.has(el.dataset.connection)) { el.open = true; const more = el.querySelector('.more-connections'); if (more) more.open = opened.get(el.dataset.connection); } });
        surface.replaceWith(content);
      }
    } catch (_) { /* Keep the saved view during connection recovery. */ }
  }
  if (screen?.refresh_url) refreshDetail();
  else if (screen?.chart_url) fetch(screen.chart_url).then(r=>r.json()).then(p=>draw(p.points)).catch(()=>draw(screen.series));
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
