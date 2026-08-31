import type { ScanResult, ScanRow } from './node';

export const LOCAL_SCAN_MAX_AGE_MS = 15 * 60 * 1000;

export interface SourcedScanRow extends ScanRow {
  source_id: string;
  source_name: string;
  source_color: string;
}

const SOURCE_COLORS = [
  '#38bdf8', '#f59e0b', '#f472b6', '#22d3ee', '#fb7185', '#a3e635', '#f97316', '#2dd4bf',
];

export function sourceColor(sourceId: string): string {
  if (sourceId === 'rati-cloud') return '#a78bfa';
  if (sourceId === 'built-in-scanner') return '#60e594';
  let hash = 0;
  for (const character of sourceId) hash = ((hash * 31) + character.charCodeAt(0)) >>> 0;
  return SOURCE_COLORS[hash % SOURCE_COLORS.length];
}

export function sourcedRows(receipts: ScanResult[]): SourcedScanRow[] {
  const newestBySource = new Map<string, ScanResult>();
  for (const receipt of [...receipts].sort(
    (left, right) => Date.parse(right.finished_at) - Date.parse(left.finished_at),
  )) {
    const sourceId = receipt.source_id || 'built-in-scanner';
    if (!newestBySource.has(sourceId)) newestBySource.set(sourceId, receipt);
  }
  const rows = [...newestBySource.values()].flatMap((receipt) => {
    const sourceId = receipt.source_id || 'built-in-scanner';
    return receipt.rows.map((row) => ({
      ...row,
      source_id: sourceId,
      source_name: receipt.source_name || 'Built-in scanner',
      source_color: sourceColor(sourceId),
    }));
  });
  return rows.sort((left, right) => right.score - left.score);
}

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

export function sourceLabel(receipt: ScanResult): string {
  return receipt.source_name || 'Built-in scanner';
}
