import { describe, expect, it } from 'vitest';

import type { ScanResult } from './node';
import {
  LOCAL_SCAN_MAX_AGE_MS,
  latestLocalScan,
  localRadarRows,
  localScanNeedsRefresh,
  sourceColor,
  sourcedRows,
} from './local-feed';

function receipt(id: string, finishedAt: string): ScanResult {
  return {
    id,
    status: 'complete',
    source: 'live',
    finished_at: finishedAt,
    elapsed_seconds: 2,
    rows: [
      {
        ticker: 'SLOW', score: 8, rug_score: 10, rug_level: 'LOW', trade_state: 'WATCH',
        price: 2, change_pct: 12, relative_volume: 1.2, state_reason: 'Price move',
      },
      {
        ticker: 'FAST', score: 7, rug_score: 20, rug_level: 'LOW', trade_state: 'WATCH',
        price: 3, change_pct: 4, relative_volume: 4.5, state_reason: 'Volume move',
      },
    ],
    warnings: [],
  };
}

describe('local scanner feed', () => {
  it('selects the newest local receipt and refreshes stale data', () => {
    const old = receipt('old', '2026-08-30T10:00:00Z');
    const fresh = receipt('fresh', '2026-08-30T10:10:00Z');
    const now = Date.parse('2026-08-30T10:12:00Z');

    expect(latestLocalScan(old, [fresh])).toBe(fresh);
    expect(localScanNeedsRefresh(fresh, now)).toBe(false);
    expect(localScanNeedsRefresh(fresh, now + LOCAL_SCAN_MAX_AGE_MS + 1)).toBe(true);
  });

  it('combines the newest receipt from each scanner source', () => {
    const value = receipt('scan', '2026-08-30T10:10:00Z');
    const remote = { ...receipt('remote', '2026-08-30T10:11:00Z'), source_id: 'remote:one', source_name: 'Desk node' };

    expect(sourcedRows([value, remote]).map((item) => item.source_name).sort()).toEqual([
      'Built-in scanner', 'Built-in scanner', 'Desk node', 'Desk node',
    ]);
    expect(localRadarRows(value).map((item) => item.ticker)).toEqual(['FAST', 'SLOW']);
  });

  it('uses fixed identity colors for built-in and RATi sources', () => {
    expect(sourceColor('built-in-scanner')).toBe('#60e594');
    expect(sourceColor('rati-cloud')).toBe('#a78bfa');
    expect(sourceColor('remote:one')).toBe(sourceColor('remote:one'));
  });
});
