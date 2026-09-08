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
    for (const [title, notices] of [['Correction', [...(corrections || [])].reverse()], ['Disclosure', disclosures]]) {
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

})();
