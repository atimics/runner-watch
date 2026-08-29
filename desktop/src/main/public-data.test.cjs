const assert = require('node:assert/strict');
const test = require('node:test');

const { safePublicDataUrl } = require('./public-data.cjs');

test('allows only the public RATi feed endpoints', () => {
  assert.equal(
    safePublicDataUrl('/api/pulse?offset=0&limit=20'),
    'https://runners.rati.chat/api/pulse?offset=0&limit=20',
  );
  assert.equal(safePublicDataUrl('/api/radar'), 'https://runners.rati.chat/api/radar');
  assert.equal(
    safePublicDataUrl('/api/flash/record'),
    'https://runners.rati.chat/api/flash/record',
  );
});

test('rejects other hosts, routes, and unbounded pagination', () => {
  assert.equal(safePublicDataUrl('https://example.com/api/pulse'), null);
  assert.equal(safePublicDataUrl('/api/private'), null);
  assert.equal(safePublicDataUrl('/api/radar?debug=1'), null);
  assert.equal(safePublicDataUrl('/api/pulse?limit=1000'), null);
  assert.equal(safePublicDataUrl('/api/pulse?limit=20&limit=21'), null);
});
