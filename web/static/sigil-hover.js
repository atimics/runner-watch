(() => {
  'use strict';
  const selector = '[data-sigil-hint]';
  const tip = document.createElement('div');
  tip.id = 'sigil-tooltip';
  tip.className = 'sigil-tooltip';
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  document.body.append(tip);
  let mark = null, owner = null, dismissed = null, hideTimer;
  let pointerFocus = false;
  const find = target => target instanceof Element ? target.closest(selector) : null;
  const focusMark = target => find(target) || (target instanceof Element && target.matches('a.ticker')
    ? target.querySelector(selector) : null);
  const models = new WeakMap();
  const number = value => Number.isFinite(value) ? new Intl.NumberFormat('en-US', {maximumFractionDigits:2}).format(value) : 'unavailable';
  const money = value => Number.isFinite(value) ? `$${number(value)}` : 'unavailable';
  const contribution = part => part && `${part.label} · ${number(part.value)} points · ${number(part.share * 100)}% of contributions`;

  function model(glyph) {
    if (!models.has(glyph)) {
      try { models.set(glyph, JSON.parse(glyph.dataset.sigilSegments)); }
      catch (_) { models.set(glyph, null); }
    }
    return models.get(glyph);
  }
  function stockReading(glyph, data, event) {
    const face = glyph.querySelector('.indicator-glyph__face'), pie = glyph.querySelector('.score-pie');
    if (!face || !pie) return null;
    if (event.target.closest('.indicator-glyph__risk')) return {key:'risk', text:data.risk};
    const bounds = face.getBoundingClientRect();
    const x = event.clientX - bounds.left - bounds.width / 2;
    const y = event.clientY - bounds.top - bounds.height / 2;
    const radius = Math.hypot(x, y), outer = bounds.width / 2;
    if (radius >= outer - 3 && radius <= outer + 1) return {key:'sentiment', text:data.sentiment};
    const pieRadius = pie.getBoundingClientRect().width / 2;
    // The existing CSS ring masks its center at 46–47% of the corner radius.
    const inner = glyph.dataset.band === '3' ? 0 : pieRadius * Math.SQRT2 * .465;
    if (radius < inner || radius > pieRadius) return null;
    const angle = ((Math.atan2(y, x) + Math.PI / 2 + Math.PI * 2) % (Math.PI * 2)) / (Math.PI * 2) * 100;
    const part = data.slices?.find(slice => slice.share > 0 && angle >= slice.start && angle < slice.end);
    return part ? {key:part.key, text:contribution(part)} : null;
  }
  function wellReading(glyph, data, event) {
    const svg = glyph.querySelector('.well-glyph'), matrix = svg?.getScreenCTM();
    if (!matrix) return null;
    const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse());
    const well = data.liquidity;
    const shapes = [...svg.querySelectorAll('circle,path,polyline')].reverse();
    for (const shape of shapes) {
      if (shape.matches('.well-wedge,.well-split,.well-cut')) continue;
      const style = getComputedStyle(shape);
      const hit = (style.fill !== 'none' && shape.isPointInFill(point))
        || (style.stroke !== 'none' && shape.isPointInStroke(point));
      if (!hit) continue;
      if (shape.matches('.well-risk')) return {key:'risk', text:data.risk};
      if (shape.matches('.well-slice')) {
        const part = data.slices?.find(slice => slice.key === shape.dataset.key && slice.share > 0);
        return part ? {key:part.key, text:contribution(part)} : null;
      }
      if (shape.matches('.well-bull,.well-bear')) {
        const side = shape.matches('.well-bull') ? 'buyers' : 'sellers';
        return {key:side, text:`Last hour · ${number(data.flow?.[side])} ${side}`};
      }
      if (shape.matches('.well-standard')) {
        const index = [...svg.querySelectorAll('.well-standard')].indexOf(shape);
        const standard = data.standards?.marks?.[index];
        return standard ? {key:`standard-${standard.key}`, text:`${standard.label} · ${{met:'Met',unmet:'Not met',unchecked:'Not checked yet'}[standard.state]}`} : null;
      }
      if (shape.matches('.well-wall')) {
        const reading = {
          sealed:'Liquidity tokens burned or locked',
          open:`${number(well.lock_left_pct)}% of liquidity tokens still held`,
          unread:'Liquidity lock not readable', unchecked:'Liquidity lock not checked yet',
          curve:'Bonding curve · no pool to lock',
        }[well.lock];
        return {key:'lock', text:reading};
      }
      if (shape.matches('.well-crack')) return {key:'withdrawal', text:'Liquidity pull on the record'};
      if (shape.matches('.well-ghost,.well-phantom')) return {key:'quoted-liquidity', text:`Quoted liquidity · ${money(well.quoted)}; real liquidity · ${money(well.real)}`};
      if (shape.matches('.well-line')) return {key:'liquidity-standard', text:`RATi pool standard · ${money(well.line_usd)} real liquidity`};
      if (shape.matches('.well-water')) return {key:'liquidity', text:`Real liquidity · ${money(well.real)}`};
      if (shape.matches('.well-chamber')) return {key:'valuation', text:`Fully diluted value · ${money(well.fdv)}`};
      if (shape.matches('.well-track')) return null;
    }
    // Unknown liquidity is a question mark in the center rather than a filled pool.
    if (event.target.closest('.well-unknown')) return {key:'liquidity', text:'Real liquidity · unavailable'};
    return null;
  }
  function pointerReading(glyph, event) {
    const data = glyph.matches('.indicator-glyph[data-sigil-segments]') && model(glyph);
    return data ? (data.liquidity ? wellReading(glyph, data, event) : stockReading(glyph, data, event)) : null;
  }

  function position() {
    if (!mark?.isConnected || tip.hidden) return;
    const anchor = mark.closest('.map-glyph') || mark;
    const rect = anchor.getBoundingClientRect(), bounds = tip.getBoundingClientRect(), gap = 10, margin = 12;
    // Prefer the side, like a label on the mark; move above when neither side fits.
    let left = rect.right + gap, top = rect.top + (rect.height - bounds.height) / 2;
    if (left + bounds.width > innerWidth - margin) left = rect.left - bounds.width - gap;
    if (left < margin) {
      left = rect.left + (rect.width - bounds.width) / 2;
      top = rect.top - bounds.height - gap;
      if (top < margin) top = rect.bottom + gap;
    }
    tip.style.left = `${Math.max(margin, Math.min(left, innerWidth - bounds.width - margin))}px`;
    tip.style.top = `${Math.max(margin, Math.min(top, innerHeight - bounds.height - margin))}px`;
  }
  function hide() {
    clearTimeout(hideTimer);
    mark?.removeAttribute('data-sigil-lit');
    mark?.removeAttribute('data-sigil-part');
    if (owner) {
      const ids = (owner.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== tip.id);
      if (ids.length) owner.setAttribute('aria-describedby', ids.join(' '));
      else owner.removeAttribute('aria-describedby');
    }
    tip.hidden = true;
    mark = owner = null;
  }
  function show(next, trigger, reading = null) {
    clearTimeout(hideTimer);
    if (!next || next === dismissed || !next.dataset.sigilHint) return;
    if (mark !== next || owner !== trigger) hide();
    mark = next; owner = trigger;
    // Other pages keep the native fallback. Suppress it only once this preview runs.
    mark.removeAttribute('title');
    mark.querySelectorAll('title').forEach(title => title.remove());
    mark.setAttribute('data-sigil-lit', '');
    if (reading) mark.dataset.sigilPart = reading.key;
    else mark.removeAttribute('data-sigil-part');
    const text = reading?.text || mark.dataset.sigilHint;
    if (tip.textContent !== text) tip.textContent = text;
    const ids = new Set((owner.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
    ids.add(tip.id); owner.setAttribute('aria-describedby', [...ids].join(' '));
    tip.hidden = false;
    position();
  }
  function leave() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      if (tip.matches(':hover') || mark?.matches(':hover') || owner?.matches(':focus-visible')) return;
      hide();
    }, 160);
  }
  function pointerPreview(event) {
    if (event.pointerType === 'touch' || !matchMedia('(hover: hover)').matches) return;
    const next = find(event.target);
    if (next) show(next, next.closest('a,button,[tabindex]') || next, pointerReading(next, event));
  }
  document.addEventListener('pointerover', pointerPreview);
  document.addEventListener('pointermove', pointerPreview);
  document.addEventListener('pointerout', event => {
    const previous = find(event.target);
    if (previous && !previous.contains(event.relatedTarget)) {
      if (dismissed === previous) dismissed = null;
      leave();
    }
  });
  tip.addEventListener('pointerenter', () => clearTimeout(hideTimer));
  tip.addEventListener('pointerleave', leave);
  document.addEventListener('pointerdown', () => { pointerFocus = true; hide(); }, true);
  document.addEventListener('keydown', event => {
    pointerFocus = false;
    if (['Enter', ' '].includes(event.key) && focusMark(event.target)) {
      // Activation opens the existing evidence view. Its Escape action stays intact.
      pointerFocus = true;
      hide();
    }
    if (event.key === 'Escape' && !tip.hidden) {
      dismissed = mark;
      hide();
      // Dismiss the preview without changing an existing map selection or focus.
      event.preventDefault(); event.stopImmediatePropagation();
    }
    if (event.key === 'Escape') pointerFocus = true;
  }, true);
  document.addEventListener('focusin', event => {
    if (!pointerFocus) show(focusMark(event.target), event.target);
  });
  document.addEventListener('focusout', event => {
    if (focusMark(event.target) === dismissed) dismissed = null;
    leave();
  });
  window.addEventListener('scroll', position, true);
  window.addEventListener('resize', position);
  // List refreshes replace row nodes. Delegated handlers also cover their replacements.
  new MutationObserver(() => { if (mark && !mark.isConnected) hide(); })
    .observe(document.body, {childList:true, subtree:true});
})();
