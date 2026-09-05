(() => {
  'use strict';

  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };

  function render(disclosures = [], corrections = []) {
    const section = element('section', null, 'content-notices');
    section.setAttribute('aria-label', 'Disclosures and corrections');
    for (const [title, notices] of [['Correction', corrections], ['Disclosure', disclosures]]) {
      for (const notice of notices || []) {
        const article = element('article', null, 'content-notice');
        article.dataset.noticeId = notice.id;
        const label = title === 'Disclosure' && notice.label && notice.label !== title ? `${title} · ${notice.label}` : title;
        article.append(element('strong', label, 'content-notice-label'), element('p', notice.text));
        if (notice.reason) article.append(element('p', `Reason: ${notice.reason}`, 'content-notice-reason'));
        const meta = element('small', null, 'content-notice-time');
        const time = element('time', `${String(notice.created_at || '').slice(0, 16).replace('T', ' ')} UTC`);
        time.dateTime = notice.created_at || '';
        meta.append(time);
        if (notice.recorded_by) meta.append(document.createTextNode(` · ${notice.recorded_by}`));
        article.append(meta);
        section.append(article);
      }
    }
    section.hidden = !section.childElementCount;
    return section;
  }

  window.RatiContentNotices = {render};

  document.querySelectorAll('[data-report-disclosure]').forEach(form => {
    let pending = false;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (pending || !form.reportValidity()) return;
      pending = true;
      const payload = {
        disclosure_kind: form.elements.disclosure_kind.value,
        disclosure: form.elements.disclosure.value.trim(),
      };
      const fieldset = form.querySelector('fieldset');
      const status = form.querySelector('[data-disclosure-status]');
      fieldset.disabled = true;
      status.textContent = 'Saving disclosure…';
      const controller = new AbortController();
      const timeout = window.setTimeout(() => controller.abort(), 10000);
      try {
        const response = await fetch(form.action, {
          method: 'POST', credentials: 'same-origin', signal: controller.signal,
          headers: {'Content-Type': 'application/json', Accept: 'application/json'},
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Save failed. Try again.');
        if (!Array.isArray(result.disclosures) || !Array.isArray(result.corrections)) throw new Error('The reply is incomplete. Refresh to check the record.');
        document.querySelector('[data-report-notices]')?.replaceChildren(render(result.disclosures, result.corrections));
        form.reset();
        status.textContent = 'Disclosure saved.';
      } catch (error) {
        status.textContent = error.name === 'AbortError' ? 'The reply is delayed. Try Save disclosure again.' : error.message || 'Save failed. Try again.';
      } finally {
        window.clearTimeout(timeout);
        fieldset.disabled = false;
        pending = false;
      }
    });
  });
})();
