(() => {
  'use strict';
  const node = document.getElementById('screenData');
  let screen = node ? JSON.parse(node.textContent) : null;
  const chart = document.querySelector('.price-chart');
  const status = document.querySelector('[data-chart-status]');
  const put = (selector, value) => { const el = document.querySelector(selector); if (el) el.textContent = value || ''; };
  function draw(points) {
    if (!chart) return;
    const ordered = new Map();
    (points || []).forEach(p => { const t = Date.parse(p.time); if (Number.isFinite(t) && Number.isFinite(p.value) && p.value > 0) ordered.set(t,p.value); });
    const data = [...ordered].sort((a,b)=>a[0]-b[0]);
    if (data.length < 2) { status.hidden = false; status.textContent = 'Price history will appear here.'; chart.setAttribute('hidden', ''); return; }
    const low = Math.min(...data.map(p=>p[1])), high = Math.max(...data.map(p=>p[1]));
    const start = data[0][0], end = data[data.length-1][0];
    const coords = data.map(p=>[8 + (p[0]-start)/(end-start)*784, high===low ? 140 : 260-(p[1]-low)/(high-low)*240]);
    const line = coords.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(' ');
    chart.querySelector('.chart-line').setAttribute('d',line);
    chart.querySelector('.chart-area').setAttribute('d',`${line} L792,280 L8,280 Z`);
    chart.removeAttribute('hidden'); status.hidden = true;
    const label = t => new Date(t).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
    put('[data-chart-start]',label(start)); put('[data-chart-end]',label(end));
  }
  draw(screen?.series);
  if (screen?.chart_url) fetch(screen.chart_url).then(r=>{if(!r.ok) throw new Error(); return r.json();}).then(p=>draw(p.points)).catch(()=>draw(screen.series));
  async function refreshQuote() {
    if (!screen?.quote_url || document.hidden) return;
    try {
      const r = await fetch(screen.quote_url); if (!r.ok) return;
      const item = await r.json(); put('[data-value]',item.value); put('[data-change]',item.change); put('[data-time]',item.time);
      const change = document.querySelector('[data-change]'); if(change) change.className = ['up','down','neutral'].includes(item.tone) ? item.tone : 'neutral';
    } catch (_) { /* Preserve the last visible quote and its time. */ }
  }
  async function refreshSurface() {
    if (document.hidden || (screen && screen.market !== 'sports')) return;
    const surface = document.querySelector('[data-live-surface]');
    if (!surface || surface.contains(document.activeElement) || document.querySelector('dialog[open]')) return;
    try {
      const response = await fetch(location.href);
      if (!response.ok || response.redirected) return;
      const next = new DOMParser().parseFromString(await response.text(), 'text/html');
      const content = next.querySelector('[data-live-surface]');
      if (!content || document.querySelector('dialog[open]') || surface.contains(document.activeElement)) return;
      if (content.innerHTML !== surface.innerHTML) surface.replaceWith(content);
      const data = next.getElementById('screenData');
      if (data) screen = JSON.parse(data.textContent);
    } catch (_) { /* Keep the current screen available while the connection recovers. */ }
  }
  refreshQuote();
  setInterval(() => { refreshQuote(); refreshSurface(); }, 60000);
  let action = null;
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    const dialog = document.querySelector('.call-confirm');
    if (button.matches('[data-action]')) {
      action = screen.actions[Number(button.dataset.action)];
      put('[data-confirm-title]', action.label); dialog.showModal(); return;
    }
    if (button.matches('[data-cancel]')) { dialog.close(); return; }
    if (!button.matches('[data-confirm]') || !action) return;
    button.disabled = true;
    try {
      const r = await fetch(action.endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(action.body)});
      if (!r.ok) { put('[data-action-status]',r.status===401 ? 'Log in to make a Call.' : 'Please try again when the price is current.'); dialog.close(); return; }
      location.reload();
    } catch (_) { put('[data-action-status]','Please try again in a moment.'); dialog.close(); }
    finally { button.disabled = false; }
  });
})();
