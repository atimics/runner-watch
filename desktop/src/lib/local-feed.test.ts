import { describe, expect, it } from 'vitest';

import type { ScanResult } from './node';
import {
  LOCAL_SCAN_MAX_AGE_MS,
  latestLocalScan,
  localRadarRows,
  localRunnerRow,
  localScanNeedsRefresh,
  shouldFetchCloudFeeds,
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
  it('never treats Local mode as permission to fetch cloud feeds', () => {
    expect(shouldFetchCloudFeeds('local')).toBe(false);
    expect(shouldFetchCloudFeeds('cloud')).toBe(true);
  });

  it('selects the newest local receipt and refreshes stale data', () => {
    const old = receipt('old', '2026-08-30T10:00:00Z');
    const fresh = receipt('fresh', '2026-08-30T10:10:00Z');
    const now = Date.parse('2026-08-30T10:12:00Z');

    expect(latestLocalScan(old, [fresh])).toBe(fresh);
    expect(localScanNeedsRefresh(fresh, now)).toBe(false);
    expect(localScanNeedsRefresh(fresh, now + LOCAL_SCAN_MAX_AGE_MS + 1)).toBe(true);
  });

  it('builds local ticker rows without cloud labels and ranks Radar by activity', () => {
    const value = receipt('scan', '2026-08-30T10:10:00Z');
    const row = localRunnerRow(value.rows[0]);

    expect(row.source).toBe('local_scanner');
    expect(row.pulse_label).toBeUndefined();
    expect(localRadarRows(value).map((item) => item.ticker)).toEqual(['FAST', 'SLOW']);
  });
});
