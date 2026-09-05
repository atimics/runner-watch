import { describe, expect, it } from 'vitest';
import { callMarkExpired, priceHistorySegments, quoteExpired } from './market-view';
import type { CoinCall, CoinRow } from './node';

const start = Date.parse('2026-09-05T16:00:00Z');
const sample = (minutes: number, price: number) => ({ observed_at: new Date(start + minutes * 60_000).toISOString(), price });

describe('saved market evidence', () => {
  it('spaces chart samples by source time', () => {
    const [segment] = priceHistorySegments([sample(0, 1), sample(1, 2), sample(10, 3)]);
    expect(segment.points).toBe('10,170 88,95 790,20');
  });

  it('breaks history gaps over ten minutes and keeps flat prices centered', () => {
    const segments = priceHistorySegments([sample(0, 2), sample(2, 2), sample(13, 2), sample(15, 2)]);
    expect(segments.map(segment => segment.count)).toEqual([2, 2]);
    expect(segments.flatMap(segment => segment.points.split(' ')).every(point => point.endsWith(',95'))).toBe(true);
  });

  it('keeps isolated observations visible and ignores unusable samples', () => {
    const segments = priceHistorySegments([sample(0, 1), sample(20, 2), { observed_at: 'unknown', price: 3 }, sample(21, NaN)]);
    expect(segments.map(segment => segment.count)).toEqual([1, 1]);
    expect(segments[1].x).toBe(790);
    expect(priceHistorySegments([sample(0, 1)])).toEqual([]);
  });

  it('ages active Call marks while preserving closed results', () => {
    const call: CoinCall = { public_id: 'active-call', coin_id: 'dogecoin', symbol: 'DOGE', name: 'Dogecoin', caller_handle: 'wolf', status: 'active', entry_price_label: '$1', mark_price_label: '$1.12', mark_at: new Date(start).toISOString(), return_pct: 12.3 };
    expect(callMarkExpired(call, start + 15 * 60_000)).toBe(false);
    expect(callMarkExpired(call, start + 15 * 60_000 + 1)).toBe(true);
    expect(callMarkExpired({ ...call, mark_at: null }, start)).toBe(true);
    expect(callMarkExpired({ ...call, status: 'closed' }, start + 60 * 60_000)).toBe(false);
  });

  it('ages quotes using both observation and collection times', () => {
    const coin = { stale: false, observed_at: new Date(start).toISOString() } as CoinRow;
    expect(quoteExpired(coin, coin.observed_at, start + 10 * 60_000)).toBe(false);
    expect(quoteExpired(coin, coin.observed_at, start + 16 * 60_000)).toBe(true);
    expect(quoteExpired(coin, new Date(start - 20 * 60_000).toISOString(), start)).toBe(true);
  });
});
