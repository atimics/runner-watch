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
  node.addEventListener('rati:screen-detail', event => {
    const assessment = event.detail.item?.assessment || {};
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
    panel.querySelector('[data-assessment-drivers]').replaceChildren(...(assessment.drivers || []).map(driver => {
      const row = document.createElement('li');
      const label = document.createElement('span');
      label.textContent = driver.label;
      row.append(label);
      if (driver.value !== null && driver.value !== undefined) {
        if (driver.unit === '%') {
          const meter = document.createElement('meter');
          meter.min = 0; meter.max = 100; meter.value = driver.value;
          meter.setAttribute('aria-label', `${driver.label} ${driver.value}%`);
          row.append(meter);
        }
        const value = document.createElement('strong');
        value.textContent = `${driver.value}${driver.unit || ''}`;
        row.append(value);
      }
      if (driver.source_url) {
        const url = new URL(driver.source_url, location.origin);
        if (url.protocol === 'https:' || url.protocol === 'http:') {
          const link = document.createElement('a');
          link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
          link.textContent = 'Source ↗'; row.append(link);
        }
      }
      return row;
    }));
    const risks = assessment.risks || [];
    panel.querySelector('[data-assessment-evidence]').hidden = !risks.length;
    panel.querySelector('[data-assessment-risks]').replaceChildren(...risks.map(text => {
      const item = document.createElement('li'); item.textContent = text; return item;
    }));
  });
})();
