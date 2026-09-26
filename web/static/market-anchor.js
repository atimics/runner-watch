(() => {
  'use strict';
  const node = document.getElementById('screenData');
  const panel = document.querySelector('[data-market-assessment]');
  const copy = document.querySelector('[data-copy-contract]');
  copy?.addEventListener('click', async () => {
    const status = document.querySelector('[data-copy-status]');
    try {
      await navigator.clipboard.writeText(copy.dataset.contract);
      status.textContent = 'Contract address copied.';
      copy.textContent = 'Copied';
    } catch (_) {
      const address = document.querySelector('[data-contract-fallback]');
      if (address) address.hidden = false;
      status.textContent = 'Select the contract address to copy it.';
    }
  });
  if (!node || !panel) return;
  const sourceLink = (href, text) => {
    if (!href) return null;
    try {
      const url = new URL(href, location.origin);
      if (!['https:', 'http:'].includes(url.protocol)) return null;
      const link = document.createElement('a');
      link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.textContent = text;
      return link;
    } catch (_) { return null; }
  };
  const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
  // Token evidence sits under the stock-style note line; the note itself is
  // refreshed by market-screen.js like a stock's.
  function render(next) {
    const assessment = next.item?.assessment || {};
    const findings = assessment.drivers || [], checks = assessment.missing_checks || [], watch = assessment.risks || [];
    const details = panel.querySelector('details');
    const focus = document.activeElement && panel.contains(document.activeElement) ? document.activeElement.href || 'summary' : null;
    details.querySelector('summary').textContent = 'Token evidence · ' + (findings.length ? plural(findings.length, 'finding') : checks.length ? 'checks awaiting data' : watch.length ? 'what to watch' : 'no chain findings saved');
    [...details.children].forEach(child => { if (child.tagName !== 'SUMMARY') child.remove(); });
    if (assessment.coverage_note) {
      const note = document.createElement('p'); note.textContent = assessment.coverage_note; details.append(note);
    }
    const list = document.createElement('ul');
    findings.forEach(driver => {
      const row = document.createElement('li');
      row.dataset.findingId = driver.id || driver.key || '';
      row.append(driver.label + (driver.value !== null && driver.value !== undefined ? ` · ${driver.value}${driver.unit || ''}` : ''));
      const receipts = driver.evidence || [];
      const links = receipts.length ? receipts.map((receipt, index) => sourceLink(receipt.receipt_url || receipt.source_url, `${receipt.kind} ${index + 1} ↗`)) : [sourceLink(driver.source_url, 'Source ↗')];
      links.filter(Boolean).forEach(link => row.append(' ', link));
      list.append(row);
    });
    checks.forEach(text => { const row = document.createElement('li'); row.textContent = `Awaiting data: ${text}`; list.append(row); });
    watch.forEach(text => { const row = document.createElement('li'); row.textContent = `Watch: ${text}`; list.append(row); });
    details.append(list);
    if (focus) {
      const target = focus === 'summary' ? details.querySelector('summary') : [...details.querySelectorAll('a')].find(link => link.href === focus);
      (target || details.querySelector('summary')).focus({preventScroll: true});
    }
  }
  node.addEventListener('rati:screen-detail', event => render(event.detail));
  if (node.ratiScreenDetail) render(node.ratiScreenDetail);
})();
