(() => {
  const desktop = window.matchMedia('(min-width: 900px)');

  function panelUrl(href) {
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) return null;
    const supported = /^\/t\/[^/]+\/?$/.test(url.pathname)
      || /^\/research\/[^/]+\/?$/.test(url.pathname)
      || /^\/s\/[^/]+\/?$/.test(url.pathname);
    return supported ? url : null;
  }

  document.querySelectorAll('[data-desktop-workspace]').forEach(workspace => {
    const list = workspace.querySelector('[data-desktop-list]');
    const frame = workspace.querySelector('[data-desktop-frame]');
    const empty = workspace.querySelector('[data-desktop-empty]');
    if (!list || !frame || !empty) return;

    let current = '';

    function defaultUrl() {
      const preferred = list.querySelector('a[data-desktop-default], a[href^="/t/"], a[href^="/research/"], a[href^="/s/"]');
      return preferred ? panelUrl(preferred.href) : null;
    }

    function markSelected(url) {
      list.querySelectorAll('a.desktop-panel-selected').forEach(link => link.classList.remove('desktop-panel-selected'));
      list.querySelectorAll('a[href]').forEach(link => {
        const candidate = panelUrl(link.href);
        if (candidate && candidate.pathname === url.pathname && candidate.hash === url.hash) {
          link.classList.add('desktop-panel-selected');
        }
      });
    }

    function openPanel(url) {
      if (!desktop.matches || !url) return;
      const value = `${url.pathname}${url.search}${url.hash}`;
      if (current !== value) frame.src = value;
      current = value;
      frame.hidden = false;
      empty.hidden = true;
      markSelected(url);
    }

    list.addEventListener('click', event => {
      if (!desktop.matches || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      const link = event.target.closest('a[href]');
      if (!link || !list.contains(link) || link.target === '_blank') return;
      const url = panelUrl(link.href);
      if (!url) return;
      event.preventDefault();
      openPanel(url);
    });

    function initialize() {
      if (!desktop.matches) return;
      openPanel(current ? panelUrl(current) : defaultUrl());
    }

    desktop.addEventListener('change', initialize);
    initialize();
  });
})();
