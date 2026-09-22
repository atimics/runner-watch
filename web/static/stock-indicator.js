/* Refresh the separate ticker check from accepted detail responses.
   The ticker map owns its glyph; no second summary panel or polling loop. */
(() => {
  'use strict';
  const node = document.getElementById('screenData');
  if (!node) return;
  const initial = node.ratiScreenDetail || JSON.parse(node.textContent || '{}');
  const ticker = initial.market === 'stocks' ? initial.item?.id : null;
  if (!ticker) return;
  const svg = (tag, attrs) => {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    return el;
  };
  node.addEventListener('rati:screen-detail', event => {
    const next = event.detail;
    if (next?.market !== 'stocks' || next.item?.id !== ticker) return;
    const item = next.item, verification = item.indicator?.verification;
    const verified = item.verified === true && verification?.basis === 'automated_evidence_gate';
    const note = (verification?.note || '') + (verification?.as_of ? ` As of ${verification.as_of}.` : '');
    for (const slot of document.querySelectorAll('[data-stock-verification]')) {
      slot.hidden = !verified;
      slot.replaceChildren();
      if (!verified) continue;
      const badge = svg('svg', {class:'ticker-verified',viewBox:'0 0 20 20',role:'img','aria-label':'Verified evidence — automated'});
      const title = svg('title', {}); title.textContent = note;
      badge.append(title, svg('circle', {cx:10,cy:10,r:9}), svg('path', {d:'m6 10 2.5 2.5 5.5-5.5'}));
      slot.append(badge);
    }
  });
})();
