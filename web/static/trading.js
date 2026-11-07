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
  const approval = document.querySelector('.trade-approval');
  const approveButton = approval.querySelector('[data-approve-trade]');
  const approvalStatus = approval.querySelector('[data-approval-status]');
  let reviewed, reviewButton;
  async function proposalRequest(id, action, token = '') {
    const response = await fetch(`/api/trading/proposals/${encodeURIComponent(id)}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, credentials: 'same-origin',
      body: JSON.stringify({action, review_token: token})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'Please refresh the trade review.');
    return data;
  }
  root.querySelectorAll('[data-proposal-action]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    status.textContent = 'Checking the trade…';
    try {
      const data = await proposalRequest(button.dataset.proposal, button.dataset.proposalAction);
      if (button.dataset.proposalAction === 'reject') { window.location.reload(); return; }
      reviewed = data;
      reviewButton = button;
      const buying = data.side === 'buy';
      approval.querySelector('[data-approval-terms]').textContent =
        `${buying ? 'Spend' : 'Sell'} ${data.input_amount} ${buying ? 'USDC' : 'tokens'} for at least ` +
        `${data.minimum_amount} ${buying ? 'tokens' : 'USDC'}. Maximum network cost: ` +
        `${data.max_fee_lamports / 1e9} SOL. This uses ${data.balance_share_percent}% of ` +
        `${buying ? 'the available USDC balance' : 'this position'}.`;
      approval.querySelector('[data-approval-address]').textContent = `Token: ${data.mint}. Wallet: ${data.wallet}.`;
      approval.querySelector('[data-approval-reason]').textContent = `${data.thesis} View changes if: ${data.invalidation}`;
      approval.querySelector('[data-approval-expiry]').textContent =
        `These limits expire at ${new Date(data.expires_at).toLocaleTimeString()}. Each sale has its own approval.`;
      approvalStatus.textContent = '';
      approveButton.disabled = false;
      status.textContent = '';
      approval.showModal();
      approval.querySelector('[data-cancel-approval]').focus();
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  }));
  approval.querySelector('[data-cancel-approval]').addEventListener('click', () => approval.close());
  approval.addEventListener('close', () => reviewButton?.focus());
  approveButton.addEventListener('click', async () => {
    if (!reviewed) return;
    approveButton.disabled = true;
    approvalStatus.textContent = 'Saving your approval…';
    try {
      await proposalRequest(reviewed.proposal_id, 'approve', reviewed.review_token);
      window.location.reload();
    } catch (error) {
      approvalStatus.textContent = error.message;
      approveButton.disabled = false;
    }
  });
})();
