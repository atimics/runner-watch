(function () {
  const root = document.querySelector('[data-market-map]');
  const dataNode = document.getElementById('marketMapData');
  if (!root || !dataNode) return;
  let data;
  try { data = JSON.parse(dataNode.textContent); } catch (_) { return; }

  const svg = root.querySelector('[data-map-canvas]');
  const empty = root.querySelector('[data-map-empty]');
  const panel = document.querySelector('[data-map-panel]');
  const panelBody = panel?.querySelector('[data-map-panel-body]');
  const NS = 'http://www.w3.org/2000/svg';

  const node = (tag, attrs) => {
    const el = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([key, value]) => el.setAttribute(key, value));
    return el;
  };

  const actors = new Map((data.actors || []).map((actor) => [actor.id, actor]));
  const subjects = data.subjects || [];
  if (!subjects.length) {
    empty.hidden = false;
    svg.setAttribute('hidden', '');
    return;
  }

  const maxWeight = Math.max(1, ...(data.actors || []).map((actor) => actor.weight || 0));
  const radiusFor = (actor) => {
    const weight = actor.weight || 0;
    return 16 + 18 * Math.sqrt(weight / maxWeight);
  };

  const cols = Math.max(1, Math.ceil(Math.sqrt(subjects.length)));
  const cell = 340;
  const rows = Math.ceil(subjects.length / cols);
  svg.setAttribute('viewBox', `0 0 ${cols * cell} ${rows * cell}`);
  svg.removeAttribute('hidden');

  subjects.forEach((subject, index) => {
    const gx = index % cols;
    const gy = Math.floor(index / cols);
    const cx = gx * cell + cell / 2;
    const cy = gy * cell + cell / 2;
    const members = subject.actor_ids.map((id) => actors.get(id)).filter(Boolean);
    const count = Math.max(1, members.length);
    const ring = Math.min(cell * 0.36, 70 + count * 9);

    const group = node('g', {class: 'map-cluster', 'data-subject': subject.key});
    const link = node('a', {href: subject.url, class: 'map-subject-link'});
    link.append(node('circle', {cx, cy, r: 30, class: 'map-subject-bubble'}));
    const label = node('text', {x: cx, y: cy + 46, class: 'map-subject-label', 'text-anchor': 'middle'});
    label.textContent = subject.label;
    link.append(label);
    group.append(link);

    members.forEach((actor, memberIndex) => {
      const angle = (memberIndex / count) * Math.PI * 2 - Math.PI / 2;
      const ax = cx + Math.cos(angle) * ring;
      const ay = cy + Math.sin(angle) * ring;
      const r = radiusFor(actor);
      group.append(node('line', {x1: cx, y1: cy, x2: ax, y2: ay, class: 'map-link'}));
      const bubble = node('g', {
        class: 'map-actor',
        'data-actor': actor.id,
        tabindex: '0',
        role: 'button',
        'aria-label': actor.name,
      });
      bubble.append(node('circle', {cx: ax, cy: ay, r, class: 'map-actor-bubble'}));
      const initial = node('text', {x: ax, y: ay + 5, class: 'map-actor-initial', 'text-anchor': 'middle'});
      initial.textContent = (actor.name || '?').trim().charAt(0).toUpperCase();
      bubble.append(initial);
      bubble.addEventListener('click', () => openActor(actor.id));
      bubble.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openActor(actor.id); }
      });
      group.append(bubble);
    });
    svg.append(group);
  });

  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => (
    {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]
  ));

  const avatarMarkup = (avatar, extraClass) => {
    if (avatar && avatar.portrait) {
      return `<span class="comment-avatar comment-avatar-portrait ${extraClass}"><img src="${escapeHtml(avatar.portrait)}" alt="" width="256" height="256"></span>`;
    }
    const tone = avatar?.tone ?? 0, frame = avatar?.frame ?? 0;
    const eyes = avatar?.eyes ?? 0, signal = avatar?.signal ?? 0;
    return `<span class="comment-avatar comment-avatar-ai avatar-tone-${tone} avatar-frame-${frame} avatar-eyes-${eyes} avatar-signal-${signal} ${extraClass}"><i></i></span>`;
  };

  const portraitOrAvatar = (actor) => {
    const avatar = actor.avatar || {};
    if (actor.portrait_ready && actor.portrait_url) {
      return `<span class="comment-avatar comment-avatar-portrait"><img src="${escapeHtml(actor.portrait_url)}" alt="" width="256" height="256" loading="lazy"></span>`;
    }
    return avatarMarkup(avatar, '');
  };

  const directionWord = (direction) => (
    {buy: 'bought', sell: 'sold', own: 'disclosed', hold: 'reported'}[direction] || 'reported'
  );

  async function openActor(actorId) {
    if (!panel || !panelBody) return;
    panel.hidden = false;
    panelBody.innerHTML = '<p class="map-panel-status">Loading character…</p>';
    let detail;
    try {
      const response = await fetch(`/api/market-actors/${encodeURIComponent(actorId)}`, {headers: {'Accept': 'application/json'}});
      if (!response.ok) throw new Error('failed');
      detail = await response.json();
    } catch (_) {
      panelBody.innerHTML = '<p class="map-panel-status">This character is not available right now.</p>';
      return;
    }
    renderActor(detail);
  }

  function renderActor(detail) {
    const actor = detail.actor;
    const roles = (actor.roles || []).join(' · ');
    const evidence = (detail.evidence || []).map((entry) => {
      const title = escapeHtml(entry.title || entry.subject_key);
      const meta = `${escapeHtml(directionWord(entry.direction))} ${entry.subject_key ? '· ' + escapeHtml(entry.subject_key) : ''}`;
      const link = entry.url
        ? `<a href="${escapeHtml(entry.url)}" target="_blank" rel="noopener noreferrer">${title}</a>`
        : title;
      return `<li><strong>${link}</strong><small>${meta} · ${escapeHtml((entry.as_of || '').slice(0, 10))}</small></li>`;
    }).join('');
    const comments = (detail.comments || []).map((comment) => `
      <li class="map-comment">
        <div class="map-comment-head">${avatarMarkup(comment.avatar, 'comment-avatar-small')}<strong>${escapeHtml(comment.alias)}</strong><small>${escapeHtml((comment.created_at || '').slice(0, 16).replace('T', ' '))}</small></div>
        <p>${escapeHtml(comment.body)}</p>
      </li>`).join('');
    panelBody.innerHTML = `
      <header class="map-panel-head">
        ${portraitOrAvatar(actor)}
        <div>
          <h2>${escapeHtml(actor.name)}</h2>
          <small>Fictional character · ${escapeHtml(actor.domain === 'coin' ? 'wallet cluster' : 'stock filer')}${roles ? ' · ' + escapeHtml(roles) : ''}</small>
        </div>
      </header>
      <p class="map-panel-note">This is an AI character built from public ${actor.domain === 'coin' ? 'on-chain evidence' : 'SEC filings'}. It is not the real person and it never speaks for them.</p>
      <div class="map-panel-actions">
        <button type="button" data-map-comment="${escapeHtml(actor.id)}">Ask for a comment</button>
        <span data-map-comment-status role="status"></span>
      </div>
      <h3>Evidence</h3>
      <ul class="map-evidence">${evidence || '<li><small>No linked evidence yet.</small></li>'}</ul>
      <h3>Comments</h3>
      <ul class="map-comments" data-map-comments>${comments || '<li><small>No comments yet.</small></li>'}</ul>
    `;
  }

  panel?.querySelector('[data-map-close]')?.addEventListener('click', () => { panel.hidden = true; });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && panel) panel.hidden = true; });

  panel?.addEventListener('click', async (event) => {
    const trigger = event.target.closest('[data-map-comment]');
    if (!trigger) return;
    const status = panel.querySelector('[data-map-comment-status]');
    trigger.disabled = true;
    if (status) status.textContent = 'Writing…';
    try {
      const response = await fetch(`/api/market-actors/${encodeURIComponent(trigger.dataset.mapComment)}/comment`, {
        method: 'POST',
        headers: {'Accept': 'application/json', 'Idempotency-Key': crypto.randomUUID()},
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail || 'Could not post a comment.');
      const list = panel.querySelector('[data-map-comments]');
      if (list && payload.comment) {
        const comment = payload.comment;
        const item = document.createElement('li');
        item.className = 'map-comment';
        item.innerHTML = `<div class="map-comment-head">${avatarMarkup(comment.avatar, 'comment-avatar-small')}<strong>${escapeHtml(comment.alias)}</strong><small>just now</small></div><p>${escapeHtml(comment.body)}</p>`;
        if (list.querySelector('small')) list.innerHTML = '';
        list.prepend(item);
      }
      if (status) status.textContent = '';
    } catch (error) {
      if (status) status.textContent = error.message || 'Could not post a comment.';
    } finally {
      trigger.disabled = false;
    }
  });
})();
