(() => {
  const thread = document.querySelector('[data-sports-comments]');
  if (!thread) return;

  const eventId = thread.dataset.eventId;
  const form = document.getElementById('sportsCommentForm');
  const body = document.getElementById('sportsCommentBody');
  const status = document.getElementById('sportsCommentStatus');
  const list = document.getElementById('commentList');
  const count = document.getElementById('discussionCount');
  const empty = document.getElementById('commentEmpty');

  function relativeTime(value) {
    const timestamp = new Date(value).valueOf();
    if (!Number.isFinite(timestamp)) return '';
    const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
    if (seconds < 60) return 'now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return days < 7
      ? `${days}d ago`
      : new Date(value).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function refreshTimes() {
    document.querySelectorAll('[data-comment-time]').forEach(item => {
      item.textContent = relativeTime(item.dateTime);
    });
  }

  function renderComment(comment) {
    const item = document.createElement('li');
    item.dataset.commentId = comment.id;

    const avatarData = comment.avatar || {};
    const avatar = document.createElement('span');
    avatar.className = [
      'comment-avatar',
      'comment-avatar-ai',
      `avatar-tone-${avatarData.tone ?? 0}`,
      `avatar-frame-${avatarData.frame ?? 0}`,
      `avatar-eyes-${avatarData.eyes ?? 0}`,
      `avatar-signal-${avatarData.signal ?? 0}`,
    ].join(' ');
    avatar.setAttribute('aria-hidden', 'true');
    avatar.append(document.createElement('i'));

    const copy = document.createElement('div');
    copy.className = 'comment-copy';
    const heading = document.createElement('header');
    const author = document.createElement('strong');
    author.textContent = avatarData.name || comment.alias || 'Unknown Signal';
    heading.append(author);
    const ability = document.createElement('span');
    ability.className = 'comment-ability';
    ability.textContent = avatarData.ability || 'Research Lens';
    if (avatarData.ability_description) ability.title = avatarData.ability_description;
    heading.append(ability);
    if (comment.is_owner) {
      const owner = document.createElement('span');
      owner.className = 'comment-owner';
      owner.textContent = 'You';
      heading.append(owner);
    }

    const meta = document.createElement('small');
    meta.className = 'comment-meta';
    const time = document.createElement('time');
    time.dateTime = comment.created_at;
    time.dataset.commentTime = '';
    time.textContent = relativeTime(comment.created_at);
    meta.append(time);
    if (comment.is_owner) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.dataset.deleteComment = comment.id;
      remove.textContent = 'delete';
      meta.append(document.createTextNode(' · '), remove);
    }

    const message = document.createElement('p');
    message.textContent = comment.body;
    copy.append(heading, meta, message);
    item.append(avatar, copy);
    return item;
  }

  function updateCounter() {
    if (!body || !status) return;
    status.textContent = `${Math.max(0, 500 - body.value.length)} characters left`;
  }

  body?.addEventListener('input', updateCounter);
  refreshTimes();

  form?.addEventListener('submit', async event => {
    event.preventDefault();
    const message = body.value.trim();
    if (!message) {
      status.textContent = 'Write a comment first.';
      body.focus();
      return;
    }
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    status.textContent = 'Posting…';
    try {
      const response = await fetch(
        `/api/sports/games/${encodeURIComponent(eventId)}/comments`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ body: message }),
        }
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || 'Could not post comment.');
      list.prepend(renderComment(result.comment));
      count.textContent = result.count;
      empty.hidden = true;
      body.value = '';
      updateCounter();
    } catch (error) {
      status.textContent = error.message || 'Could not post comment.';
    } finally {
      button.disabled = false;
    }
  });

  list?.addEventListener('click', async event => {
    const button = event.target.closest('[data-delete-comment]');
    if (!button) return;
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/sports/comments/${encodeURIComponent(button.dataset.deleteComment)}`,
        { method: 'DELETE' }
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail || 'Could not delete comment.');
      button.closest('li')?.remove();
      count.textContent = Math.max(0, Number(count.textContent || 0) - 1);
      empty.hidden = Boolean(list.children.length);
    } catch (error) {
      if (status) status.textContent = error.message || 'Could not delete comment.';
      button.disabled = false;
    }
  });
})();
