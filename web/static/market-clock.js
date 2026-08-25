(() => {
  const clocks = [...document.querySelectorAll('[data-market-clock]')];
  if (!clocks.length) return;

  function duration(seconds) {
    const value = Math.max(0, Math.floor(seconds));
    const days = Math.floor(value / 86400);
    const hours = Math.floor(value % 86400 / 3600);
    const minutes = Math.floor(value % 3600 / 60);
    const secs = value % 60;
    if (days) return `${days}d ${hours}h`;
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  function tick() {
    clocks.forEach(clock => {
      const remaining = (new Date(clock.dataset.nextAt).getTime() - Date.now()) / 1000;
      clock.querySelector('[data-session-countdown]').textContent = `${clock.dataset.nextLabel} in ${duration(remaining)}`;
    });
  }

  function apply(clock, data) {
    clock.dataset.nextAt = data.next_at;
    clock.dataset.nextLabel = data.next_label;
    clock.className = `session-clock session-${data.session}`;
    clock.querySelector('[data-session-label]').textContent = data.label;
    clock.querySelector('[data-session-note]').textContent = data.data_note;
    clock.querySelectorAll('[data-session-key]').forEach(item => item.classList.toggle('active', item.dataset.sessionKey === data.session));
  }

  tick();
  setInterval(tick, 1000);
  setInterval(async () => {
    try {
      const response = await fetch('/api/market-clock');
      if (!response.ok) return;
      const data = await response.json();
      clocks.forEach(clock => apply(clock, data));
      tick();
    } catch (_) {}
  }, 60000);
})();
