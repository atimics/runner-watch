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
      reward.textContent = result.reward_status === 'reserved' ? `You earn ${result.reward_amount} Flash when all ratification checks pass.` : 'The discovery reward is reserved for the first submitter.';
      button.hidden = true;
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
  const screen = document.querySelector('#screenData');
  screen?.addEventListener('rati:screen-detail', event => {
    if (document.querySelector('.token-sigil-pending') && !event.detail.lookup_pending) location.reload();
    const result = event.detail.discovery;
    if (!result) return;
    const state = document.querySelector('[data-discovery-state]');
    const checks = document.querySelector('[data-discovery-checks]');
    if (state) state.textContent = {review:'Under review', tracked:'Tracked pool', rejected:'Rejected pool'}[result.status];
    if (checks) checks.textContent = `${result.met} of ${result.total} ratification checks passed.`;
    if (reward && result.reward_status === 'paid') reward.textContent = `${result.reward_amount} Flash added to your balance.`;
  });
})();
