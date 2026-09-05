import { afterEach, describe, expect, it, vi } from 'vitest';

import { NodeClient, normalizeNodeUrl } from './node';

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe('scanner node addresses', () => {
  it('explains missing and invalid scanner addresses', () => {
    expect(() => normalizeNodeUrl('')).toThrow('Scanner address is unavailable');
    expect(() => normalizeNodeUrl('not an address')).toThrow('Enter a valid scanner address');
  });

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

  it('sends the scanner token without putting it in the URL', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ api_version: '1' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await new NodeClient('https://scanner.example.com', 'private-token').node();

    expect(fetchMock).toHaveBeenCalledWith(
      'https://scanner.example.com/api/v1/node',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer private-token' }),
      }),
    );
  });

  it('starts only live scans against the connected sources', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'complete', source: 'live', rows: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await new NodeClient('http://127.0.0.1:8787', 'private-token').liveScan();

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8787/api/v1/scans',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ universe: 'penny', min_price: 0.2, max_price: 5, top_n: 20 }),
      }),
    );
    expect(fetchMock.mock.calls[0][1]?.body).not.toContain('sample');
  });

  it('loads ticker detail from the selected scanner', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ ticker: 'BRK-B', source: 'local_scanner' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await new NodeClient('http://127.0.0.1:8787', 'private-token').ticker('BRK.B');

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8787/api/v1/tickers/BRK.B',
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: 'Bearer private-token' }),
      }),
    );
  });

  it('stops waiting when a scanner does not respond', async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, 'fetch').mockImplementation((_input, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));

    const request = new NodeClient('https://scanner.example.com').node();
    const expectation = expect(request).rejects.toThrow('Scanner request timed out');
    await vi.advanceTimersByTimeAsync(12_000);
    await expectation;
  });
});

describe('shared market routes', () => {
  it.each([
    ['coin list', (client: NodeClient) => client.memecoins('doge & coin', 'gainers'), '/api/v1/markets/memecoins?q=doge+%26+coin&sort=gainers'],
    ['coin detail', (client: NodeClient) => client.memecoin('same-symbol-two'), '/api/v1/markets/memecoins/coins/same-symbol-two'],
    ['coin Calls', (client: NodeClient) => client.memecoinCalls(), '/api/v1/markets/memecoins/calls'],
    ['Sports Radar', (client: NodeClient) => client.sports('radar'), '/api/v1/markets/sports/radar'],
  ] as const)('loads %s through the authenticated node', async (_label, load, path) => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('{}', { status: 200 }));
    await load(new NodeClient('http://127.0.0.1:8787', 'private-token'));
    expect(fetchMock).toHaveBeenCalledWith(`http://127.0.0.1:8787${path}`, expect.objectContaining({
      headers: expect.objectContaining({ Authorization: 'Bearer private-token' }),
    }));
  });
});
