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
  const focusMark = target => find(target) || (target instanceof Element ? target.querySelector(selector) : null);

  function position() {
    if (!mark?.isConnected || tip.hidden) return;
    const rect = mark.getBoundingClientRect(), bounds = tip.getBoundingClientRect(), gap = 10, margin = 12;
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
    if (owner) {
      const ids = (owner.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== tip.id);
      if (ids.length) owner.setAttribute('aria-describedby', ids.join(' '));
      else owner.removeAttribute('aria-describedby');
    }
    tip.hidden = true;
    mark = owner = null;
  }
  function show(next, trigger) {
    clearTimeout(hideTimer);
    if (!next || next === dismissed || !next.dataset.sigilHint) return;
    if (mark !== next || owner !== trigger) hide();
    mark = next; owner = trigger;
    mark.setAttribute('data-sigil-lit', '');
    tip.textContent = mark.dataset.sigilHint;
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
  document.addEventListener('pointerover', event => {
    if (event.pointerType === 'touch' || !matchMedia('(hover: hover)').matches) return;
    const next = find(event.target);
    if (next) show(next, next.closest('a,button,[tabindex]') || next);
  });
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
    if (event.key === 'Escape' && !tip.hidden) {
      dismissed = mark;
      hide();
      // Dismiss the preview without changing an existing map selection or focus.
      event.preventDefault(); event.stopImmediatePropagation();
    }
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
