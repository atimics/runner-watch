(() => {
  const desktop = window.matchMedia('(min-width: 900px)');

  function panelUrl(href) {
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) return null;
    const supported = /^\/t\/[^/]+\/?$/.test(url.pathname)
      || /^\/research\/[^/]+\/?$/.test(url.pathname)
      || /^\/s\/[^/]+\/?$/.test(url.pathname)
      || /^\/(?:sports\/)?game\/[^/]+\/?$/.test(url.pathname)
      || /^\/memecoins\/(?!radar\/?$|alpha\/?$)[^/]+\/?$/.test(url.pathname);
    return supported ? url : null;
  }

  document.querySelectorAll('[data-desktop-workspace]').forEach(workspace => {
    const list = workspace.querySelector('[data-desktop-list]');
    const frame = workspace.querySelector('[data-desktop-frame]');
    const empty = workspace.querySelector('[data-desktop-empty]');
    const loading = workspace.querySelector('[data-desktop-loading]');
    if (!list || !frame || !empty || !loading) return;

    let current = '';
    let loaded = false;

    function defaultUrl() {
      const preferred = list.querySelectorAll('a[data-desktop-default], a[href^="/t/"], a[href^="/research/"], a[href^="/s/"], a[href^="/game/"], a[href^="/sports/game/"], a[href^="/memecoins/"]');
      return Array.from(preferred).map(link => panelUrl(link.href)).find(Boolean) || null;
    }

    function markSelected(url) {
      let selectedLink = null;
      list.querySelectorAll('a.desktop-panel-selected').forEach(link => link.classList.remove('desktop-panel-selected'));
      list.querySelectorAll('a[href]').forEach(link => {
        const candidate = panelUrl(link.href);
        if (candidate && candidate.pathname === url.pathname && candidate.hash === url.hash) {
          link.classList.add('desktop-panel-selected');
          selectedLink = link;
        }
      });
      if (selectedLink) {
        requestAnimationFrame(() => {
          const rowBounds = selectedLink.getBoundingClientRect();
          const listBounds = list.getBoundingClientRect();
          if (rowBounds.top < listBounds.top || rowBounds.bottom > listBounds.bottom) {
            selectedLink.scrollIntoView({block: 'nearest'});
          }
        });
      }
    }

    function openPanel(url) {
      if (!desktop.matches || !url) return;
      const value = `${url.pathname}${url.search}${url.hash}`;
      if (current !== value) {
        current = value;
        loaded = false;
        frame.hidden = true;
        loading.hidden = false;
        empty.hidden = true;
        frame.src = value;
      } else if (loaded) {
        frame.hidden = false;
        loading.hidden = true;
        empty.hidden = true;
      }
      markSelected(url);
    }

    frame.addEventListener('load', () => {
      if (!current || frame.getAttribute('src') !== current) return;
      loaded = true;
      frame.hidden = false;
      loading.hidden = true;
      empty.hidden = true;
    });

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
    list.addEventListener('desktop-rows-rendered', initialize);
    initialize();
  });
})();
