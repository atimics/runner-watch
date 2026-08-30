(() => {
  'use strict';

  function mount(options) {
    const refresh = options.refreshButton;
    if (!refresh) return null;
    let pending = null;
    let polling = false;

    function applyPending() {
      if (!pending) return;
      const current = options.getCurrent();
      const next = options.apply ? options.apply(current, pending) : pending;
      options.setCurrent(next);
      pending = null;
      refresh.hidden = true;
      options.render(next);
      options.afterApply?.(next);
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
          refresh.hidden = true;
          const merged = options.onNoChange?.(next, current);
          if (merged) options.setCurrent(merged);
          return;
        }
        pending = next;
        const count = options.changeCount?.(current, next) ?? 0;
        refresh.textContent = options.label(count, current, next);
        refresh.hidden = false;
      } catch (error) {
        options.onError?.(error);
      } finally {
        polling = false;
      }
    }

    refresh.addEventListener('click', applyPending);
    options.render(options.getCurrent());
    const timer = setInterval(poll, options.interval || 30000);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) poll();
    });
    return {
      poll,
      applyPending,
      stop: () => clearInterval(timer),
      hasPending: () => Boolean(pending),
    };
  }

  window.RatiLiveList = {mount};
})();
