import { afterEach, describe, expect, it, vi } from 'vitest';

import { RATi_RUNNERS_URL, RunnersClient } from './runners';

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('RATi Runners public data', () => {
  it('uses the hosted RATi Runners service by default', () => {
    expect(new RunnersClient().baseUrl).toBe(RATi_RUNNERS_URL);
  });

  it('rejects insecure remote data services', () => {
    expect(() => new RunnersClient('http://example.com')).toThrow(
      'RATi Runners data must use HTTPS',
    );
  });

  it('loads Pulse without sending scanner credentials', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ rows: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await new RunnersClient().pulse();

    expect(fetchMock).toHaveBeenCalledWith(
      'https://runners.rati.chat/api/pulse?offset=0&limit=20',
      expect.objectContaining({ headers: { Accept: 'application/json' } }),
    );
    const init = fetchMock.mock.calls[0][1];
    expect(init?.headers).not.toHaveProperty('Authorization');
  });

  it('stops waiting when the public feed does not respond', async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));

    const request = new RunnersClient().radar();
    const expectation = expect(request).rejects.toThrow('RATi Runners request timed out');
    await vi.advanceTimersByTimeAsync(12_000);
    await expectation;
  });
});
