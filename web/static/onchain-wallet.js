(() => {
  const root = document.querySelector('[data-chain-wallet]');
  if (!root) return;
  const button = root.querySelector('[data-wallet-refresh]');
  const status = root.querySelector('[data-wallet-status]');
  let busy = false;
  async function refresh() {
    if (busy) return;
    busy = true;
    button.disabled = true;
    status.textContent = 'Reading chain history and holdings…';
    try {
      const response = await fetch('/api/wallets/' + encodeURIComponent(root.dataset.walletId) + '/refresh', {
        method: 'POST', headers: {'Accept': 'application/json'}, credentials: 'same-origin'
      });
      if (!response.ok) throw new Error('Please try again in a minute.');
      const payload = await response.json();
      if (payload.status === 'ready' && !payload.error) {
        location.reload();
        return;
      }
      status.textContent = payload.error || 'Chain data is waiting for the next read. Please try again soon.';
    } catch (error) {
      status.textContent = error.message || 'Please try again in a minute.';
    }
    busy = false;
    button.disabled = false;
  }
  button.addEventListener('click', refresh);
  if (root.dataset.status === 'pending' && root.dataset.providerReady === 'true') refresh();
})();
