(() => {
  'use strict';

  function mount(options) {
    const refresh = options.refreshButton;
    if (!refresh) return null;
    let pending = null;
    let polling = false;
    let focusKeys = null;

    function focusLabel(count) {
      return options.focusLabel
        ? options.focusLabel(count)
        : `Showing ${count} new · tap for all`;
    }

    function showFocus() {
      refresh.textContent = focusLabel(focusKeys.length);
      refresh.setAttribute('aria-pressed', 'true');
      refresh.dataset.focused = 'true';
      refresh.hidden = false;
    }

    function clearFocus(render) {
      const wasFocused = Boolean(focusKeys);
      focusKeys = null;
      refresh.removeAttribute('aria-pressed');
      delete refresh.dataset.focused;
      if (wasFocused && render) {
        const current = options.getCurrent();
        options.render(current, null);
        options.afterApply?.(current);
      }
      return wasFocused;
    }

    function applyPending() {
      if (!pending) return;
      const current = options.getCurrent();
      const added = options.newKeys?.(current, pending) ?? null;
      const next = options.apply ? options.apply(current, pending) : pending;
      options.setCurrent(next);
      pending = null;
      if (added && added.length) {
        focusKeys = [...new Set([...(focusKeys || []), ...added])];
        showFocus();
      } else {
        clearFocus(false);
        refresh.hidden = true;
      }
      options.render(next, focusKeys);
      options.afterApply?.(next);
    }

    function toggle() {
      if (pending) {
        applyPending();
        return;
      }
      if (clearFocus(true)) refresh.hidden = true;
    }

    async function poll() {
      if (polling || document.hidden) return;
      polling = true;
      try {
        const next = await options.fetchNext();
        const current = options.getCurrent();
        options.onPoll?.(next, current);
        if (!options.changed(current, next)) {
          pending = null;
          if (focusKeys) showFocus();
          else refresh.hidden = true;
          const merged = options.onNoChange?.(next, current);
          if (merged) options.setCurrent(merged);
          return;
        }
        pending = next;
        const count = options.changeCount?.(current, next) ?? 0;
        refresh.textContent = options.label(count, current, next);
        refresh.removeAttribute('aria-pressed');
        delete refresh.dataset.focused;
        refresh.hidden = false;
      } catch (error) {
        options.onError?.(error);
      } finally {
        polling = false;
      }
    }

    refresh.addEventListener('click', toggle);
    options.render(options.getCurrent(), null);
    const timer = setInterval(poll, options.interval || 30000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) poll();
    });
    return {
      poll,
      applyPending,
      toggle,
      stop: () => clearInterval(timer),
      hasPending: () => Boolean(pending),
      focusedKeys: () => (focusKeys ? [...focusKeys] : null),
    };
  }

  window.RatiLiveList = {mount};
})();
