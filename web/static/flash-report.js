(() => {
  const REPORT_COST = 100;
  const POLL_TIMEOUT_MS = 330000;
  const FAILURE_MESSAGE = "Report couldn't be generated. No Flash was charged.";
  const UNAVAILABLE_MESSAGE = 'Report unavailable. Try again later.';

  function setAction(button, label, detail) {
    const strong = button.querySelector('strong');
    const secondary = button.querySelector('span, small');
    if (strong) strong.textContent = label;
    if (secondary) secondary.textContent = detail;
  }

  function setStatus(status, message, tone = '') {
    if (!status) return;
    status.textContent = message;
    if (tone) status.dataset.tone = tone;
    else delete status.dataset.tone;
  }

  function updateBalance(payload) {
    const balance = Number(payload?.balance);
    if (!Number.isFinite(balance)) return;
    if (window.RatiFlash?.updateBalance) {
      window.RatiFlash.updateBalance(balance);
      return;
    }
    document.querySelectorAll('[data-flash-balance]').forEach(item => {
      item.textContent = String(balance);
    });
  }

  function showGenerating(button, status) {
    button.disabled = true;
    button.dataset.flashReportState = 'running';
    setAction(button, 'Generating report…', 'This may take a minute');
    setStatus(status, '');
  }

  function showFailure(button, status, payload = {}) {
    updateBalance(payload);
    const retryable = payload.retryable !== false;
    button.disabled = !retryable;
    button.dataset.flashReportState = retryable ? 'failed' : 'unavailable';
    setAction(
      button,
      retryable ? 'Try again' : 'Report unavailable',
      retryable ? `${REPORT_COST} Flash · private 1h` : 'Try again later'
    );
    setStatus(status, FAILURE_MESSAGE, 'error');
  }

  function startFailureMessage(statusCode) {
    if (statusCode === 402) return `You need ${REPORT_COST} Flash to generate this report.`;
    if (statusCode === 409) return 'This report is no longer available. Refresh the page.';
    if (statusCode === 423) return "Today's report is temporarily private.";
    return UNAVAILABLE_MESSAGE;
  }

  function pollLater(callback, delay) {
    window.setTimeout(callback, delay);
  }

  async function poll(button, status, jobId, startedAt, delay = 2500) {
    if (Date.now() - startedAt >= POLL_TIMEOUT_MS) {
      button.disabled = true;
      setAction(button, 'Still generating', 'Reload to check');
      setStatus(status, 'The report is taking longer than expected.', 'error');
      return;
    }

    try {
      const response = await fetch(`/api/research/jobs/${encodeURIComponent(jobId)}`);
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error('status request failed');
      updateBalance(result);
      if (result.status === 'complete' && result.url) {
        location.href = result.url;
        return;
      }
      if (result.status === 'failed') {
        showFailure(button, status, result);
        return;
      }
      showGenerating(button, status);
    } catch (_) {
      setStatus(status, 'Checking report status…');
    }

    const nextDelay = Math.min(5000, Math.round(delay * 1.35));
    pollLater(() => poll(button, status, jobId, startedAt, nextDelay), delay);
  }

  function bind(button) {
    if (!button || button.dataset.flashReportBound === 'true') return;
    button.dataset.flashReportBound = 'true';
    const status = document.getElementById('commissionStatus');

    if (button.dataset.jobId) {
      showGenerating(button, status);
      poll(button, status, button.dataset.jobId, Date.now());
    }

    button.addEventListener('click', async () => {
      if (window.RatiFlash?.canSpend && !window.RatiFlash.canSpend(REPORT_COST)) return;
      showGenerating(button, status);
      try {
        const response = await fetch(button.dataset.startUrl, {method: 'POST'});
        const result = await response.json().catch(() => ({}));
        updateBalance(result);
        if (!response.ok) {
          button.disabled = false;
          setAction(
            button,
            button.dataset.readyLabel || 'Generate report',
            button.dataset.readyDetail || `${REPORT_COST} Flash · private 1h`
          );
          setStatus(status, startFailureMessage(response.status), 'error');
          if (response.status === 402) {
            window.RatiFlash?.handleInsufficient?.(result.detail, REPORT_COST);
          }
          return;
        }
        if (result.status === 'complete' && result.url) {
          location.href = result.url;
          return;
        }
        if (result.status === 'failed') {
          showFailure(button, status, result);
          return;
        }
        if (!result.job_id) throw new Error('missing job id');
        button.dataset.jobId = result.job_id;
        poll(button, status, result.job_id, Date.now());
      } catch (_) {
        button.disabled = false;
        setAction(button, 'Check status', 'No extra charge');
        setStatus(status, 'Could not confirm the report. Refresh this page to check.', 'error');
      }
    });
  }

  document.querySelectorAll('[data-flash-report]').forEach(bind);
  window.RatiFlashReport = {bind};
})();
