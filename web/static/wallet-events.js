/* Filings arrive in pages, but a reader should never hunt for a "next" link.
   This appends the next page when the tail comes into view, and keeps the
   button as a keyboard- and click-accessible fallback. */
(() => {
  'use strict';
  const section = document.querySelector('[data-wallet-events]');
  const more = section?.querySelector('[data-wallet-more]');
  const list = section?.querySelector('[data-wallet-event-list]');
  if (!more || !list) return;
  let cursor = more.dataset.cursor || '';
  let loading = false;
  async function load() {
    if (loading || !cursor) return;
    loading = true;
    more.setAttribute('aria-busy', 'true');
    try {
      const url = `${more.dataset.endpoint}?cursor=${encodeURIComponent(cursor)}`;
      const response = await fetch(url, {headers: {Accept: 'application/json'}});
      if (!response.ok) throw new Error('events');
      const payload = await response.json();
      const holder = document.createElement('div');
      holder.innerHTML = payload.html || '';
      list.append(...holder.children);
      cursor = payload.next_cursor || '';
      if (cursor) more.dataset.cursor = cursor;
      else more.remove();
    } catch (_) {
      more.textContent = 'Could not load older filings · tap to retry';
    } finally {
      loading = false;
      more.removeAttribute('aria-busy');
    }
  }
  more.addEventListener('click', load);
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(
      entries => { if (entries.some(entry => entry.isIntersecting)) load(); },
      {rootMargin: '300px 0px'},
    );
    observer.observe(more);
  }
})();
