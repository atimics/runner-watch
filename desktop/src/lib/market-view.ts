import type { CoinCall, CoinRow } from './node';

const QUOTE_MAX_AGE_MS = 15 * 60_000;
const HISTORY_GAP_MS = 10 * 60_000;

export function timestampExpired(value: string | null | undefined, now: number): boolean {
  const timestamp = value ? Date.parse(value) : NaN;
  return !Number.isFinite(timestamp) || now - timestamp > QUOTE_MAX_AGE_MS || timestamp - now > 60_000;
}

export function quoteExpired(coin: CoinRow, collectedAt: string | null | undefined, now: number): boolean {
  return coin.stale || timestampExpired(coin.observed_at, now) || timestampExpired(collectedAt, now);
}

export function callStatusLabel(status: CoinCall['status']): string {
  return status === 'active' ? 'Open' : 'Closed';
}

export function callMarkExpired(call: CoinCall, now: number): boolean {
  return call.status === 'active' && timestampExpired(call.mark_at, now);
}

export interface HistorySegment {
  points: string;
  count: number;
  x: number;
  y: number;
}

export function priceHistorySegments(history: { observed_at: string; price: number }[]): HistorySegment[] {
  const samples = history
    .map(point => ({ price: point.price, time: Date.parse(point.observed_at) }))
    .filter(point => Number.isFinite(point.time) && Number.isFinite(point.price) && point.price > 0)
    .sort((left, right) => left.time - right.time);
  if (samples.length < 2) return [];
  const low = Math.min(...samples.map(point => point.price));
  const high = Math.max(...samples.map(point => point.price));
  const firstTime = samples[0].time;
  const timeSpan = samples[samples.length - 1].time - firstTime;
  const segments: HistorySegment[] = [];
  samples.forEach((point, index) => {
    const x = timeSpan ? 10 + (point.time - firstTime) / timeSpan * 780 : 400;
    const y = high === low ? 95 : 170 - (point.price - low) / (high - low) * 150;
    if (index === 0 || point.time - samples[index - 1].time > HISTORY_GAP_MS) {
      segments.push({ points: `${x},${y}`, count: 1, x, y });
    } else {
      const segment = segments[segments.length - 1];
      segment.points += ` ${x},${y}`;
      segment.count += 1;
    }
  });
  return segments;
}
