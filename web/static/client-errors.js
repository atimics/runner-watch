(function () {
  const endpoint = '/api/client-errors';
  const maxReports = 5;
  const reported = new Set();
  let sent = 0;
  const release = (() => {
    try {
      const script = document.currentScript;
      return script && script.src ? new URL(script.src).searchParams.get('v') || '' : '';
    } catch (_) { return ''; }
  })();

  const clip = (value, size) => String(value == null ? '' : value).replace(/[\u0000-\u001f\u007f]/g, ' ').slice(0, size);

  function report(kind, detail) {
    try {
      if (sent >= maxReports) return;
      const payload = {
        kind: kind,
        message: clip(detail.message, 500),
        source: clip(detail.source, 300),
        line: Number.isFinite(detail.line) ? detail.line : null,
        column_number: Number.isFinite(detail.column) ? detail.column : null,
        stack: clip(detail.stack, 4000),
        page_url: location.pathname,
        release: release,
      };
      if (!payload.message) return;
      const key = payload.kind + ':' + payload.message + ':' + payload.source + ':' + (payload.line || '');
      if (reported.has(key)) return;
      reported.add(key);
      sent += 1;
      const body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        navigator.sendBeacon(endpoint, new Blob([body], {type: 'application/json'}));
      } else {
        fetch(endpoint, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: body,
          keepalive: true,
        }).catch(() => {});
      }
    } catch (_) {}
  }

  window.addEventListener('error', (event) => {
    const target = event.target;
    if (target && target !== window && target.tagName) {
      if (target.tagName === 'SCRIPT' || target.tagName === 'LINK') {
        report('resource', {message: target.tagName + ' failed to load: ' + (target.src || target.href || ''), source: target.src || target.href});
      }
      return;
    }
    const error = event.error;
    report('error', {
      message: event.message || (error && error.message) || 'Uncaught error',
      source: event.filename,
      line: event.lineno,
      column: event.colno,
      stack: error && error.stack,
    });
  }, true);

  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason;
    report('rejection', {
      message: (reason && reason.message) || String(reason || 'Unhandled promise rejection'),
      stack: reason && reason.stack,
    });
  });
})();