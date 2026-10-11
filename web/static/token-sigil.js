(() => {
  const button = document.querySelector('[data-submit-token]');
  const status = document.querySelector('[data-submission-status]');
  const reward = document.querySelector('[data-discovery-reward]');
  button?.addEventListener('click', async () => {
    button.disabled = true;
    status.textContent = 'Saving your submission…';
    try {
      const response = await fetch('/api/memecoin-discoveries/' + encodeURIComponent(button.dataset.submitToken), {method:'POST', headers:{'Accept':'application/json'}});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || 'Please try again shortly.');
      status.textContent = 'Submission saved.';
      if (result.reward_status === 'reserved') reward.textContent = `You earn ${result.reward_amount} Flash when all ratification checks pass.`;
      button.hidden = true;
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
  const screen = document.querySelector('#screenData');
  let previousState;
  try { previousState = JSON.parse(screen?.textContent || '{}').discovery?.status; } catch (_) { /* The page still supports submission. */ }
  screen?.addEventListener('rati:screen-detail', event => {
    if (document.querySelector('.token-sigil-pending') && !event.detail.lookup_pending) location.reload();
    const result = event.detail.discovery;
    if (!result) return;
    if (previousState && result.status !== previousState) location.reload();
    previousState = result.status;
    const state = document.querySelector('[data-discovery-state]');
    const checks = document.querySelector('[data-discovery-checks]');
    if (state) state.textContent = {review:'Under review', tracked:'Tracked pool', rejected:'Rejected pool'}[result.status];
    if (checks) checks.textContent = `${result.met} of ${result.total} ratification checks passed.`;
    if (reward && result.reward_status === 'paid') reward.textContent = `${result.reward_amount} Flash added to your balance.`;
  });
})();
