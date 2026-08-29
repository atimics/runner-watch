export const RATi_RUNNERS_URL = 'https://runners.rati.chat';

export interface RunnerRow {
  ticker: string;
  company?: string;
  name?: string;
  price?: number;
  change_pct?: number;
  trade_state?: string;
  stage?: string;
  source?: string;
  coin_tone?: string;
  coin_label?: string;
  entered_at?: string;
  event_at?: string;
  event_count?: number;
  rug_score?: number;
  rug_level?: string;
  sentiment?: string;
  pulse_label?: string;
  directional_thesis?: string;
  has_update?: boolean;
  case_confidence?: number;
  case_thesis?: string;
  case_source_name?: string;
  social_label?: string;
}

export interface FlashVersion {
  label: string;
  model_label: string;
  state: string;
  headline_rate_visible: boolean;
  hit_rate: number | null;
  settled: number;
  hits: number;
  misses: number;
  no_calls: number;
  pending: number;
  voids?: number;
  under_review?: number;
  distinct_tickers: number;
  distinct_trading_days?: number;
  hit_rate_interval_95?: [number, number] | null;
}

export interface FlashResult {
  report_url: string;
  ticker: string;
  direction: string;
  probability_up: number;
  classification?: string | null;
  status: string;
  return_pct?: number | null;
  version_label: string;
  correction_count?: number;
}

export interface FlashRecord {
  current_version: FlashVersion | null;
  contract: { minimum_move_pct: number; headline_sample: number };
  versions: FlashVersion[];
  recent_results: FlashResult[];
  method_note?: string;
}

export interface PulseData {
  rows: RunnerRow[];
  next_offset?: number;
  has_more?: boolean;
  updated_at?: string;
  flash_record?: FlashVersion | null;
}

export interface RadarData {
  rows: RunnerRow[];
  updated_at?: string;
}

export class RunnersClient {
  readonly baseUrl: string;

  constructor(baseUrl = RATi_RUNNERS_URL) {
    const parsed = new URL(baseUrl);
    const loopbackHttp = parsed.protocol === 'http:'
      && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost');
    if (parsed.protocol !== 'https:' && !loopbackHttp) {
      throw new Error('RATi Runners data must use HTTPS');
    }
    this.baseUrl = parsed.href.replace(/\/$/, '');
  }

  private async request<T>(path: string, timeoutMs = 12_000): Promise<T> {
    if (this.baseUrl === RATi_RUNNERS_URL && window.ratiDesktop) {
      return window.ratiDesktop.fetchPublic<T>(path);
    }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        headers: { Accept: 'application/json' },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`RATi Runners returned ${response.status}`);
      return response.json() as Promise<T>;
    } catch (error) {
      if (controller.signal.aborted) throw new Error('RATi Runners request timed out');
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  pulse(): Promise<PulseData> {
    return this.request('/api/pulse?offset=0&limit=20');
  }

  radar(): Promise<RadarData> {
    return this.request('/api/radar');
  }

  flash(): Promise<FlashRecord> {
    return this.request('/api/flash/record');
  }
}
