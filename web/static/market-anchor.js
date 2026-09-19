(() => {
  'use strict';
  const node = document.getElementById('screenData');
  const panel = document.querySelector('[data-market-assessment]');
  if (!node || !panel) return;
  const copy = document.querySelector('[data-copy-contract]');
  copy?.addEventListener('click', async () => {
    const status = document.querySelector('[data-copy-status]');
    try {
      await navigator.clipboard.writeText(copy.dataset.contract);
      status.textContent = 'Contract address copied.';
      copy.textContent = 'Copied';
    } catch (_) {
      document.querySelector('.token-contract details').open = true;
      status.textContent = 'Select the contract address to copy it.';
    }
  });
  const put = (name, value) => {
    const target = panel.querySelector(`[data-assessment-${name}]`);
    if (target) target.textContent = value ?? '';
  };
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
  function render(next) {
    const assessment = next.item?.assessment || {};
    put('label', assessment.label || 'Assessment pending');
    put('value', Number.isFinite(assessment.value) ? `${Math.round(assessment.value * 10) / 10}${assessment.unit || ''}` : '—');
    put('reason', assessment.reason);
    put('time', assessment.as_of ? `Saved ${assessment.as_of}` : '');
    const tag = panel.querySelector('[data-assessment-tag]');
    const tones = ['setup', 'running', 'extended', 'avoid', 'watch', 'paused', 'lean', 'pass', 'model-only'];
    tag.hidden = !assessment.tag;
    tag.textContent = assessment.tag || '';
    tag.className = 'tag tag-' + (tones.includes(assessment.tag_tone) ? assessment.tag_tone : 'watch');
    panel.querySelector('[data-assessment-contributions]').replaceChildren(...(assessment.contributions || []).map(part => {
      const row = document.createElement('li'), label = document.createElement('span');
      const meter = document.createElement('meter'), value = document.createElement('strong');
      label.textContent = part.label;
      const points = Number(part.value);
      value.textContent = `${points >= 0 ? '+' : ''}${points}`;
      meter.min = 0; meter.max = 100; meter.value = Math.abs(points);
      meter.setAttribute('aria-label', `${part.label} ${value.textContent} points`);
      if (points < 0) row.className = 'is-penalty';
      row.append(label, meter, value); return row;
    }));
    const expanded = new Set([...panel.querySelectorAll('[data-finding-id][open]')].map(item => item.dataset.findingId));
    panel.querySelector('[data-assessment-drivers]').replaceChildren(...(assessment.drivers || []).map(driver => {
      const row = document.createElement('li');
      const heading = document.createElement('div');
      heading.className = 'assessment-driver-summary';
      const label = document.createElement('span');
      label.textContent = driver.label;
      heading.append(label);
      row.append(heading);
      if (driver.value !== null && driver.value !== undefined) {
        if (driver.unit === '%') {
          const meter = document.createElement('meter');
          meter.min = 0; meter.max = 100; meter.value = driver.value;
          meter.setAttribute('aria-label', `${driver.label} ${driver.value}%`);
          heading.append(meter);
        }
        const value = document.createElement('strong');
        value.textContent = `${driver.value}${driver.unit || ''}`;
        heading.append(value);
      }
      const receipts = driver.evidence || [];
      if (!receipts.length) {
        const source = sourceLink(driver.source_url, 'Source ↗');
        if (source) heading.append(source);
      }
      if (receipts.length || driver.explanation) {
        const details = document.createElement('details'), summary = document.createElement('summary');
        details.className = 'assessment-finding';
        details.dataset.findingId = driver.id || driver.key || '';
        details.open = expanded.has(details.dataset.findingId);
        const basis = {pattern: 'Pattern', relationship: 'Relationship'}[driver.basis] || 'Observation';
        summary.textContent = `${basis} · ${receipts.length ? `${receipts.length} ${receipts.length === 1 ? 'receipt' : 'receipts'}` : 'Context'}`;
        details.append(summary);
        if (driver.explanation) {
          const explanation = document.createElement('p');
          explanation.textContent = driver.explanation;
          details.append(explanation);
        }
        if (receipts.length) {
          const list = document.createElement('ol');
          receipts.forEach((receipt, index) => {
            const entry = document.createElement('li');
            const saved = sourceLink(receipt.receipt_url || receipt.source_url, `${receipt.kind} ${index + 1} ↗`);
            if (saved) entry.append(saved);
            if (receipt.receipt_url && receipt.source_url && receipt.receipt_url !== receipt.source_url) {
              const explorer = sourceLink(receipt.source_url, 'Explorer ↗');
              if (explorer) entry.append(explorer);
            }
            list.append(entry);
          });
          details.append(list);
        }
        row.append(details);
      }
      return row;
    }));
    const risks = assessment.risks || [];
    panel.querySelector('[data-assessment-evidence]').hidden = !risks.length;
    panel.querySelector('[data-assessment-risks]').replaceChildren(...risks.map(text => {
      const item = document.createElement('li'); item.textContent = text; return item;
    }));
  }
  node.addEventListener('rati:screen-detail', event => render(event.detail));
  if (node.ratiScreenDetail) render(node.ratiScreenDetail);
})();
