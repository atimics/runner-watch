(() => {
  const KEY = 'runner_openrouter_key';
  const VERIFIER = 'runner_openrouter_verifier';
  const STATE = 'runner_openrouter_state';
  const RETURN_TO = 'runner_openrouter_return';

  function base64url(bytes) {
    let text = '';
    new Uint8Array(bytes).forEach(byte => { text += String.fromCharCode(byte); });
    return btoa(text).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function safeReturn(value) {
    try {
      const target = new URL(value || '/', location.origin);
      if (target.origin === location.origin) return target.pathname + target.search + target.hash;
    } catch (_) {}
    return '/';
  }

  async function connect(returnTo) {
    const verifier = base64url(crypto.getRandomValues(new Uint8Array(48)));
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
    const state = base64url(crypto.getRandomValues(new Uint8Array(24)));
    sessionStorage.setItem(VERIFIER, verifier);
    sessionStorage.setItem(STATE, state);
    sessionStorage.setItem(RETURN_TO, safeReturn(returnTo || location.href));
    const callback = new URL('/auth/openrouter/callback', location.origin);
    callback.searchParams.set('state', state);
    const auth = new URL('https://openrouter.ai/auth');
    auth.searchParams.set('callback_url', callback.toString());
    auth.searchParams.set('code_challenge', base64url(digest));
    auth.searchParams.set('code_challenge_method', 'S256');
    location.href = auth.toString();
  }

  async function finish() {
    const params = new URLSearchParams(location.search);
    const code = params.get('code');
    const state = params.get('state');
    const verifier = sessionStorage.getItem(VERIFIER);
    if (!code || !verifier || !state || state !== sessionStorage.getItem(STATE)) {
      throw new Error('OpenRouter connection expired. Start again.');
    }
    const response = await fetch('https://openrouter.ai/api/v1/auth/keys', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code, code_verifier: verifier, code_challenge_method: 'S256'})
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || !result.key) throw new Error('OpenRouter could not be connected.');
    localStorage.setItem(keyName(), result.key);
    const returnTo = safeReturn(sessionStorage.getItem(RETURN_TO));
    sessionStorage.removeItem(VERIFIER);
    sessionStorage.removeItem(STATE);
    sessionStorage.removeItem(RETURN_TO);
    return returnTo;
  }

  let profile = 'guest';
  function setProfile(value) { profile = value || 'guest'; }
  function keyName() { return `${KEY}:${profile}`; }
  function disconnect() { localStorage.removeItem(keyName()); }
  function key() { return localStorage.getItem(keyName()) || ''; }
  function connected() { return Boolean(key()); }

  window.runnerOpenRouter = {connect, finish, disconnect, key, connected, setProfile};
})();
