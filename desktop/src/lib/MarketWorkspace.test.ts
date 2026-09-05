import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MarketWorkspace from './MarketWorkspace.svelte';
import { NodeClient, type CoinCall } from './node';

let component: ReturnType<typeof mount> | null = null;
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const coin = { id: 'same-symbol-two', symbol: 'SAME', name: 'Second coin', price: 0.0000012, price_label: '$0.0000012', change_24h: 3.2, volume_label: '$3M', market_cap_label: '$8M', observed_at: '2026-09-05T16:00:00Z', stale: true };

function render(overrides: Record<string, unknown> = {}) {
  const props = { market: 'memecoins' as const, tab: 'pulse' as const, enabled: true, available: true, nodeUrl: 'http://127.0.0.1:8787', nodeToken: 'private-local-token', onconnect: vi.fn(), openExternal: vi.fn(), ...overrides };
  component = mount(MarketWorkspace, { target: document.body, props });
  flushSync();
  return props;
}

function button(text: string) {
  const result = [...document.querySelectorAll('button')].find(item => item.textContent?.includes(text));
  if (!result) throw new Error(`Missing button: ${text}`);
  return result;
}

describe('native market workspace', () => {
  it('offers the existing cloud connection before any market fetch', async () => {
    const markets = vi.spyOn(NodeClient.prototype, 'memecoins');
    const props = render({ enabled: false });
    button('Enable RATi Cloud').click();
    expect(props.onconnect).toHaveBeenCalledOnce();
    expect(markets).not.toHaveBeenCalled();
  });

  it('opens coin IDs inside the app and preserves tiny prices and saved quote status', async () => {
    vi.spyOn(NodeClient.prototype, 'memecoins').mockResolvedValue({ rows: [coin], status: 'stale', collected_at: coin.observed_at, refresh_failed: false, total: 1, source: 'CoinGecko' });
    const detail = vi.spyOn(NodeClient.prototype, 'memecoin').mockResolvedValue({ coin, status: 'stale', collected_at: coin.observed_at, refresh_failed: false, source: 'CoinGecko', history: [], evidence: { observed_at: coin.observed_at, collected_at: coin.observed_at }, in_current_snapshot: true, calls: [] });
    const props = render();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Second coin'));
    button('Second coin').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Source observed'));
    expect(detail).toHaveBeenCalledWith('same-symbol-two');
    expect(document.body.textContent).toContain('$0.0000012');
    expect(document.body.textContent).toContain('Saved quote');
    expect(props.openExternal).not.toHaveBeenCalled();
    button('Open coin and paper Calls').click();
    expect(props.openExternal).toHaveBeenCalledWith('https://runners.rati.chat/memecoins/coin/same-symbol-two');
  });

  it('shows Sports results and selected game evidence in the app', async () => {
    const sports = vi.spyOn(NodeClient.prototype, 'sports').mockResolvedValue({ events: [{ id: 'event-1', league: 'mlb', away_abbreviation: 'SEA', home_abbreviation: 'TOR', model_winner_team_name: 'Seattle', model_winner_probability_pct: 57.2, market_probability_pct: 55.1 }] });
    const props = render({ market: 'sports' });
    await vi.waitFor(() => expect(document.body.textContent).toContain('SEA at TOR'));
    button('SEA at TOR').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Projected winner:'));
    expect(sports).toHaveBeenCalledWith('pulse');
    expect(props.openExternal).not.toHaveBeenCalled();
    button('Open game evidence').click();
    expect(props.openExternal).toHaveBeenCalledWith('https://sports.rati.chat/game/event-1');
  });

  it('shows source failures and supports a refresh', async () => {
    const markets = vi.spyOn(NodeClient.prototype, 'memecoins').mockRejectedValueOnce(new Error('Source unavailable')).mockResolvedValue({ rows: [], status: 'pending', collected_at: null, refresh_failed: false, total: 0, source: 'CoinGecko' });
    render();
    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent).toBe('Source unavailable'));
    button('Refresh').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Feed status: pending'));
    expect(markets).toHaveBeenCalledTimes(2);
  });
});

describe('native market refresh', () => {
  const market = { rows: [coin], status: 'ok', collected_at: coin.observed_at, refresh_failed: false, total: 1, source: 'CoinGecko' };
  const coinDetail = { coin, status: 'ok', collected_at: coin.observed_at, refresh_failed: false, source: 'CoinGecko', history: [], evidence: { observed_at: coin.observed_at, collected_at: coin.observed_at }, in_current_snapshot: true, calls: [] };

  it('keeps selected coin evidence visible through refresh errors and updates', async () => {
    const list = vi.spyOn(NodeClient.prototype, 'memecoins').mockResolvedValue(market);
    vi.spyOn(NodeClient.prototype, 'memecoin').mockResolvedValueOnce(coinDetail).mockRejectedValueOnce(new Error('Refresh delayed')).mockResolvedValueOnce({ ...coinDetail, coin: { ...coin, price_label: '$0.0000013' } });
    render();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Second coin'));
    button('Second coin').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Source observed'));
    button('Refresh').click();
    await vi.waitFor(() => expect(document.querySelector('[role="alert"]')?.textContent).toBe('Refresh delayed'));
    expect(document.body.textContent).toContain('Source observed');
    expect(document.body.textContent).toContain('$0.0000012');
    button('Refresh').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('$0.0000013'));
    expect(document.body.textContent).toContain('Source observed');
    expect(list).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledWith('', 'volume', 'pulse');
  });

  it('keeps the selected game open as the shared feed changes', async () => {
    const row = { id: 'event-1', away_abbreviation: 'SEA', home_abbreviation: 'TOR', model_winner_team_name: 'Seattle', model_winner_probability_pct: 57.2 };
    vi.spyOn(NodeClient.prototype, 'sports').mockResolvedValueOnce({ events: [row] }).mockResolvedValueOnce({ events: [{ ...row, model_winner_probability_pct: 62.3 }] });
    render({ market: 'sports' });
    await vi.waitFor(() => expect(document.body.textContent).toContain('SEA at TOR'));
    button('SEA at TOR').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('Projected winner:'));
    button('Refresh').click();
    await vi.waitFor(() => expect(document.body.textContent).toContain('62.3%'));
    expect(document.body.textContent).toContain('Projected winner:');
  });

  it('expires active marks on the refresh clock, preserves closed returns, and clears its timer', async () => {
    vi.useFakeTimers();
    const start = Date.parse('2026-09-05T16:00:00Z');
    vi.setSystemTime(start);
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const call: CoinCall = { public_id: 'active-call', coin_id: coin.id, name: coin.name, symbol: coin.symbol, caller_handle: 'wolf', status: 'active', entry_price_label: '$1', mark_price_label: '$1.12', mark_at: new Date(start).toISOString(), return_pct: 12.3 };
    const calls = vi.spyOn(NodeClient.prototype, 'memecoinCalls').mockResolvedValueOnce({ calls: [call, { ...call, public_id: 'closed-call', status: 'closed' }] }).mockRejectedValue(new Error('Refresh delayed'));
    render({ tab: 'alpha' });
    await vi.advanceTimersByTimeAsync(0);
    flushSync();
    expect(document.body.textContent).toContain('Mark $1.12');
    vi.setSystemTime(start + 16 * 60_000);
    await vi.advanceTimersByTimeAsync(60_000);
    flushSync();
    expect(calls).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain('Mark expired');
    expect(document.body.textContent).toContain('+12.3%');
    expect(document.body.textContent).toContain('Refresh delayed');
    await unmount(component!);
    component = null;
    expect(vi.getTimerCount()).toBe(0);
  });
});
