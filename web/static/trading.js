(() => {
  const root = document.querySelector('[data-trading-mode]');
  if (!root) return;
  const mode = root.dataset.tradingMode;
  const status = root.querySelector('[data-trading-message]');
  const dialog = document.querySelector('.trade-confirm');
  let exitButton;
  async function submit(button) {
    button.disabled = true;
    status.textContent = 'Saving your request…';
    try {
      const response = await fetch(`/api/trading/control?mode=${encodeURIComponent(mode)}`, {
        method: 'POST', headers: {'Content-Type': 'application/json'}, credentials: 'same-origin',
        body: JSON.stringify({action: button.dataset.tradeAction, position_id: button.dataset.position || null})
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || 'The request needs another check.');
      window.location.reload();
    } catch (error) {
      status.textContent = typeof error.message === 'string' ? error.message : 'Please try again.';
      button.disabled = false;
    }
  }
  root.querySelector('[data-refresh]').addEventListener('click', () => window.location.reload());
  root.querySelectorAll('[data-trade-action]').forEach(button => button.addEventListener('click', () => {
    if (button.dataset.tradeAction === 'exit') {
      exitButton = button;
      dialog.showModal();
      dialog.querySelector('[data-cancel-exit]').focus();
    } else submit(button);
  }));
  dialog.querySelector('[data-cancel-exit]').addEventListener('click', () => dialog.close());
  dialog.querySelector('[data-confirm-exit]').addEventListener('click', () => {
    dialog.close();
    if (exitButton) submit(exitButton);
  });
  dialog.addEventListener('close', () => exitButton?.focus());
})();
