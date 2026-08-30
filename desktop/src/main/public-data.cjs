const RUNNERS_ORIGIN = 'https://runners.rati.chat';
const PUBLIC_DATA_PATHS = new Set(['/api/pulse', '/api/radar', '/api/flash/record']);

function hasBoundedInteger(url, key, minimum, maximum) {
  const values = url.searchParams.getAll(key);
  if (values.length === 0) return true;
  if (values.length !== 1 || !/^\d+$/.test(values[0])) return false;
  const value = Number(values[0]);
  return value >= minimum && value <= maximum;
}

function safePublicDataUrl(value) {
  try {
    const url = new URL(String(value), RUNNERS_ORIGIN);
    if (url.origin !== RUNNERS_ORIGIN || !PUBLIC_DATA_PATHS.has(url.pathname)) return null;
    if (url.pathname !== '/api/pulse' && url.search) return null;
    if (url.pathname === '/api/pulse') {
      for (const key of url.searchParams.keys()) {
        if (!['offset', 'limit'].includes(key)) return null;
      }
      if (!hasBoundedInteger(url, 'offset', 0, 10_000)) return null;
      if (!hasBoundedInteger(url, 'limit', 1, 100)) return null;
    }
    return url.href;
  } catch {
    return null;
  }
}

module.exports = { RUNNERS_ORIGIN, safePublicDataUrl };
