(() => {
  'use strict';

  const COST = 10;

  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `comment-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function relativeTime(value) {
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (!Number.isFinite(seconds)) return '';
    if (seconds < 60) return 'now';
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return new Date(value).toLocaleDateString([], {month: 'short', day: 'numeric'});
  }

  function renderComment(comment) {
    const item = document.createElement('li');
    item.dataset.commentId = comment.id;
    const avatarData = comment.avatar || {};
    const variant = (key, maximum) => {
      const value = Number(avatarData[key]);
      return Number.isInteger(value) && value >= 0 && value < maximum ? value : 0;
    };
    const avatar = document.createElement('span');
    avatar.className = [
      'comment-avatar', 'comment-avatar-ai', `avatar-tone-${variant('tone', 12)}`,
      `avatar-frame-${variant('frame', 6)}`, `avatar-eyes-${variant('eyes', 6)}`,
      `avatar-signal-${variant('signal', 6)}`,
    ].join(' ');
    avatar.setAttribute('aria-hidden', 'true');
    avatar.append(document.createElement('i'));

    const copy = document.createElement('div');
    copy.className = 'comment-copy';
    const head = document.createElement('header');
    const author = document.createElement('strong');
    author.textContent = avatarData.name || comment.alias || 'Unknown Signal';
    const ability = document.createElement('span');
    ability.className = 'comment-ability';
    ability.textContent = avatarData.ability || 'Research Lens';
    if (avatarData.ability_description) ability.title = avatarData.ability_description;
    head.append(author, ability);
    if (comment.is_owner) {
      const owner = document.createElement('span');
      owner.className = 'comment-owner';
      owner.textContent = 'You';
      head.append(owner);
    }
    const meta = document.createElement('small');
    meta.className = 'comment-meta';
    const stamp = document.createElement('time');
    stamp.dateTime = comment.created_at;
    stamp.dataset.commentTime = '';
    stamp.textContent = relativeTime(comment.created_at);
    meta.append(stamp);
    if (comment.is_owner) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.dataset.deleteComment = comment.id;
      remove.textContent = 'delete';
      meta.append(document.createTextNode(' · '), remove);
    }
    const body = document.createElement('p');
    body.textContent = comment.body;
    copy.append(head, meta, body);
    item.append(avatar, copy);
    return item;
  }

  function wire(thread) {
    const generate = thread.querySelector('[data-generate-comment]');
    const status = thread.querySelector('[data-comment-status]');
    const list = thread.querySelector('[data-comment-list]');
    const count = thread.querySelector('[data-comment-count]');
    const empty = thread.querySelector('[data-comment-empty]');
    const storageKey = `pending-comment:${thread.dataset.commentStorageKey}`;
    let pending = null;
    try { pending = sessionStorage.getItem(storageKey); } catch (_) {}

    const remember = value => {
      pending = value;
      try {
        if (value) sessionStorage.setItem(storageKey, value);
        else sessionStorage.removeItem(storageKey);
      } catch (_) {}
    };

    thread.querySelectorAll('[data-comment-time]').forEach(item => {
      item.textContent = relativeTime(item.dateTime);
    });

    generate?.addEventListener('click', async () => {
      if (window.RatiFlash?.canSpend && !window.RatiFlash.canSpend(COST)) return;
      if (!pending) remember(requestId());
      generate.disabled = true;
      status.textContent = 'Posting…';
      try {
        const response = await fetch(thread.dataset.commentEndpoint, {
          method: 'POST', headers: {'Idempotency-Key': pending},
        });
        const result = await response.json().catch(() => ({}));
        if (!response.ok) {
          if (!result.retryable) remember(null);
          if (response.status === 402) {
            window.RatiFlash?.handleInsufficient?.(result.detail, COST);
          }
          throw new Error(result.detail || 'Could not post.');
        }
        const shown = Array.from(list.children).some(
          item => item.dataset.commentId === String(result.comment.id)
        );
        if (!shown) list.prepend(renderComment(result.comment));
        status.textContent = 'Posted';
        count.textContent = result.count;
        empty.hidden = true;
        window.RatiFlash?.updateBalance?.(result.balance);
        remember(null);
      } catch (error) {
        status.textContent = error.message || 'Could not post.';
      } finally {
        generate.disabled = false;
      }
    });

    list?.addEventListener('click', async event => {
      const button = event.target.closest('[data-delete-comment]');
      if (!button) return;
      button.disabled = true;
      try {
        const response = await fetch(
          `/api/comments/${encodeURIComponent(button.dataset.deleteComment)}`,
          {method: 'DELETE'}
        );
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.detail || 'Could not delete.');
        button.closest('li')?.remove();
        count.textContent = Math.max(0, Number(count.textContent || 0) - 1);
        empty.hidden = Boolean(list.children.length);
      } catch (error) {
        status.textContent = error.message || 'Could not delete.';
        button.disabled = false;
      }
    });
  }

  document.querySelectorAll('[data-flash-comments]').forEach(wire);
})();
