/* Keep detail glyphs in step with the existing accepted-response event.
   No polling of our own, innerHTML, status-chip writes, or scoring decisions. */
(() => {
  'use strict';
  const node = document.getElementById('screenData');
  if (!node) return;
  const allowed = (value, options, fallback) => options.includes(value) ? value : fallback;
  const number = value => typeof value === 'number' && Number.isFinite(value) ? value : null;
  const text = (selector, value) => {
    const target = document.querySelector(selector);
    if (target) target.textContent = value;
  };
  const make = (tag, value) => {
    const el = document.createElement(tag);
    if (value != null) el.textContent = value;
    return el;
  };
  const svg = (tag, attrs) => {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    return el;
  };
  const titleCase = value => value[0].toUpperCase() + value.slice(1);
  function refresh(next) {
    if (next?.market !== 'stocks') return;
    const item = next.item || {}, glyph = item.indicator || {};
    const risk = allowed(glyph.risk, ['low','medium','high','unknown'], 'unknown');
    const sentiment = allowed(glyph.sentiment, ['positive','negative','neutral','unknown'], 'unknown');
    const mix = allowed(glyph.mix_state, ['available','zero','unknown'], 'unknown');
    const score = number(glyph.score);
    const parts = ['market','evidence','social'].map((key, index) => {
      const found = (Array.isArray(glyph.slices) ? glyph.slices : []).find(part => part?.key === key);
      return {key, label: ['Market','Filings + news','External social'][index], value: Math.max(0, number(found?.value) || 0)};
    });
    const total = parts.reduce((sum, part) => sum + part.value, 0);
    let cursor = 0;
    const stops = [];
    for (const part of parts) {
      part.share = total > 0 && Number.isFinite(total) ? part.value / total : 0;
      const end = cursor + 100 * part.share;
      if (part.share) stops.push(`var(--indicator-${part.key}) ${cursor}% ${end}%`);
      cursor = end;
    }
    for (const element of document.querySelectorAll('.stock-indicator-summary .indicator-glyph')) {
      element.dataset.band = String(allowed(glyph.band, [1,2,3], 1));
      element.dataset.risk = risk; element.dataset.sentiment = sentiment; element.dataset.mix = mix;
      element.setAttribute('aria-label', glyph.description || 'Attention, sentiment and risk unavailable');
      element.title = element.getAttribute('aria-label');
      element.querySelector('.score-pie').style.background = stops.length && mix === 'available' ? `conic-gradient(${stops.join(', ')})` : '';
      let dot = element.querySelector('.indicator-glyph__risk');
      if (risk === 'low') dot?.remove();
      else {
        if (!dot) { dot = make('span'); dot.className = 'indicator-glyph__risk'; element.querySelector('.indicator-glyph__face').append(dot); }
        dot.textContent = risk === 'unknown' ? '?' : '';
      }
    }
    const verified = item.verified === true && glyph.verification?.basis === 'automated_evidence_gate';
    const note = (glyph.verification?.note || '') + (glyph.verification?.as_of ? ` As of ${glyph.verification.as_of}.` : '');
    for (const slot of document.querySelectorAll('[data-stock-verification]')) {
      slot.hidden = !verified;
      slot.replaceChildren();
      if (verified) {
        const badge = svg('svg', {class:'ticker-verified',viewBox:'0 0 20 20',role:'img','aria-label':'Verified evidence — automated'});
        const title = svg('title', {}); title.textContent = note;
        badge.append(title, svg('circle', {cx:10,cy:10,r:9}), svg('path', {d:'m6 10 2.5 2.5 5.5-5.5'}));
        slot.append(badge);
      }
    }
    text('[data-indicator-attention]', score == null ? 'Attention unavailable' : `Attention ${score} points`);
    text('[data-indicator-band]', `${allowed(glyph.band_label, ['Low','Medium','High','Unknown'], 'Unknown')} attention`);
    text('[data-indicator-reading]', `Filing sentiment: ${titleCase(sentiment)} · Risk: ${titleCase(risk)}`);
    const components = document.querySelector('.indicator-components');
    if (components) {
      components.hidden = mix !== 'available';
      components.replaceChildren(...parts.map(part => {
        const group = make('div');
        group.append(make('dt', part.label), make('dd', `${part.value.toFixed(1)} pts · ${(100*part.share).toFixed(1)}% of mix`));
        return group;
      }));
    }
    const missing = document.querySelector('[data-indicator-missing]');
    if (missing) { missing.hidden = mix === 'available'; missing.textContent = mix === 'zero' ? 'No attention contributions.' : 'Attention breakdown unavailable.'; }
    const verificationNote = document.querySelector('[data-indicator-verification-note]');
    if (verificationNote) { verificationNote.hidden = !verified; verificationNote.textContent = note; }
  }
  node.addEventListener('rati:screen-detail', event => refresh(event.detail));
})();
