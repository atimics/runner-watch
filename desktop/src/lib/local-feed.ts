import type { ScanResult, ScanRow } from './node';
import type { RunnerRow } from './runners';

export const LOCAL_SCAN_MAX_AGE_MS = 15 * 60 * 1000;

export function latestLocalScan(
  selected: ScanResult | null,
  receipts: ScanResult[],
): ScanResult | null {
  const candidates = [selected, ...receipts].filter((value): value is ScanResult => value != null);
  return candidates.sort(
    (left, right) => Date.parse(right.finished_at) - Date.parse(left.finished_at),
  )[0] || null;
}

export function localScanNeedsRefresh(
  receipt: ScanResult | null,
  now = Date.now(),
): boolean {
  if (!receipt) return true;
  const finishedAt = Date.parse(receipt.finished_at);
  return !Number.isFinite(finishedAt) || now - finishedAt > LOCAL_SCAN_MAX_AGE_MS;
}

export function localRadarRows(receipt: ScanResult | null): ScanRow[] {
  if (!receipt) return [];
  return [...receipt.rows].sort((left, right) => {
    const volumeDifference = (right.relative_volume || 0) - (left.relative_volume || 0);
    if (volumeDifference) return volumeDifference;
    const moveDifference = Math.abs(right.change_pct) - Math.abs(left.change_pct);
    return moveDifference || right.score - left.score;
  });
}

export function localRunnerRow(row: ScanRow): RunnerRow {
  return {
    ticker: row.ticker,
    price: row.price,
    change_pct: row.change_pct,
    trade_state: row.trade_state,
    source: 'local_scanner',
    rug_score: row.rug_score,
    rug_level: row.rug_level,
    directional_thesis: row.state_reason,
  };
}

export function shouldFetchCloudFeeds(mode: 'local' | 'cloud'): boolean {
  return mode === 'cloud';
}
