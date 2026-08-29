import { describe, expect, it } from 'vitest';

import { normalizeNodeUrl } from './node';

describe('scanner node addresses', () => {
  it('accepts local HTTP scanner nodes', () => {
    expect(normalizeNodeUrl('http://127.0.0.1:8787/')).toBe('http://127.0.0.1:8787');
    expect(normalizeNodeUrl('http://localhost:9000')).toBe('http://localhost:9000');
  });

  it('requires HTTPS for remote scanner nodes', () => {
    expect(normalizeNodeUrl('https://cloud.rati.chat/')).toBe('https://cloud.rati.chat');
    expect(() => normalizeNodeUrl('http://scanner.example.com')).toThrow(
      'Remote scanner connections must use HTTPS',
    );
    expect(() => normalizeNodeUrl('ftp://127.0.0.1:8787')).toThrow(
      'Remote scanner connections must use HTTPS',
    );
  });
});
