(() => {
  const form = document.getElementById('channelAccess');
  if (!form) return;
  const status = document.getElementById('channelStatus');
  const labels = {pending: 'Queued', sending: 'Sending', sent: 'Delivered', retry: 'Waiting to retry', uncertain: 'Check delivery', failed: 'Needs attention'};
  const element = (tag, text) => { const node = document.createElement(tag); node.textContent = text; return node; };
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    status.textContent = 'Loading channel posts…';
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      const response = await fetch('/api/telegram/announcements', {headers: {Authorization: `Bearer ${document.getElementById('channelKey').value}`}, cache: 'no-store'});
      if (!response.ok) throw new Error('Check your access key and try again.');
      const data = await response.json();
      document.getElementById('channelLimits').textContent = `${data.limits.daily} daily attempts · ${Math.round(data.limits.interval_seconds / 60)} minutes between posts · ${Math.round(data.limits.ticker_quiet_seconds / 60)} minutes between posts about one ticker`;
      const posts = document.getElementById('channelPosts');
      posts.replaceChildren();
      for (const post of data.posts) {
        const card = element('article', ''); card.className = 'channel-post';
        const header = element('header', '');
        header.append(element('strong', labels[post.status] || post.status), element('time', new Date(post.updated_at).toLocaleString()));
        card.append(header, element('p', `${post.format === 'animation' ? 'Coin replay GIF' : 'Text update'} · Destination ${post.chat_id}`), element('pre', post.text || (post.status === 'sent' ? 'Earlier delivery · Caption recorded with the saved replay.' : 'The caption will appear when the replay is ready.')));
        if (post.message_id) card.append(element('p', `Telegram receipt ${post.message_id} · ${post.attempts} attempt${post.attempts === 1 ? '' : 's'}`));
        if (post.retry_at && post.status === 'retry') card.append(element('p', `Retry eligible after ${new Date(post.retry_at).toLocaleString()}`));
        if (post.status === 'uncertain') card.append(element('p', 'Check the chat to confirm delivery. This message is held for review.'));
        posts.append(card);
      }
      status.textContent = data.posts.length ? `${data.posts.length} recent posts` : 'New announcements will appear here as they are queued.';
      button.textContent = 'Refresh history';
    } catch (error) { status.textContent = error.message; }
    finally { button.disabled = false; }
  });
})();
