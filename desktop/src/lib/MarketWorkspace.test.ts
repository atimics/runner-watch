import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MarketWorkspace from './MarketWorkspace.svelte';
import { NodeClient } from './node';

let component: ReturnType<typeof mount> | null = null;
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
  vi.restoreAllMocks();
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
