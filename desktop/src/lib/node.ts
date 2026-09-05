export interface NodeCapabilities {
  background_service: string;
  community: string;
  research: string;
  sports: string;
  memecoins: string;
  stocks: string;
}

export interface NodeStatus {
  api_version: string;
  scanner_version: string;
  node_id: string;
  mode: 'local' | 'self_hosted' | 'cloud';
  capabilities: NodeCapabilities;
}

export interface ProviderFeed {
  id: string;
  title: string;
  schedule: string;
  review_status: string;
  terms_url: string | null;
  capabilities: string[];
  usage_rights: string[];
  access_model: string;
  storage_policy: string;
  display_policy: string;
  product: string;
}

export interface ProviderStatus {
  id: string;
  title: string;
  state: string;
  enabled: boolean;
  configured: boolean;
  configuration_kind: string;
  runtime_available?: boolean;
  capabilities: string[];
  feeds: ProviderFeed[];
}

export interface CoverageProvider {
  provider_id: string;
  provider_title: string;
  state: string;
  enabled: boolean;
  configured: boolean;
  configuration_kind: string;
  runtime_available?: boolean;
  review_status: string;
  usage_rights: string[];
  access_model: string;
  feeds: string[];
  terms_url: string | null;
}

export interface SourceCapability {
  id: string;
  title: string;
  description: string;
  core: boolean;
  private_ready: boolean;
  public_ready: boolean;
  selected_provider: string | null;
  provider_route: string[];
  providers: CoverageProvider[];
}

export interface CoveragePayload {
  summary: {
    private_ready: number;
    public_ready: number;
    total: number;
    core_private_ready: boolean;
    core_public_ready: boolean;
  };
  capabilities: SourceCapability[];
}

export interface RemoteScannerInput {
  name: string;
  url: string;
  token: string;
}

export interface OpenRouterConnection {
  status: 'connected' | 'disconnected';
  provider: 'openrouter';
  credential_owner?: string;
  connection_method?: string;
  activity_url?: string;
  settings_url?: string;
}

export interface ScanRow {
  ticker: string;
  score: number;
  rug_score: number;
  rug_level: string;
  trade_state: string;
  price: number;
  change_pct: number;
  relative_volume: number | null;
  state_reason: string;
}

export interface ScanResult {
  id: string;
  status: 'complete';
  source: 'live';
  started_at?: string;
  finished_at: string;
  elapsed_seconds: number;
  requested_symbols?: number;
  liquid_symbols?: number;
  scanned_symbols?: number;
  rows: ScanRow[];
  warnings: string[];
  source_id?: string;
  source_name?: string;
}

export interface TickerBar {
  timestamp: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number;
  volume: number | null;
}

export interface TickerPull {
  label: string;
  provider: string;
  feed: string;
  status: 'connected' | 'failed';
  bars: number;
  delayed: boolean | null;
  observed_at: string | null;
  collected_at: string | null;
  fallback_used: boolean;
  attempted_providers: string[];
}

export interface TickerAnalysis extends ScanRow {
  signals: string[];
  risks: string[];
  quote_time: string;
  session: string;
  momentum_5m_pct: number;
  momentum_15m_pct: number;
  breakout_pct: number;
  vwap_position_pct: number;
  pullback_from_high_pct: number;
  dollar_volume: number;
  average_dollar_volume: number;
  stale_minutes: number;
}

export interface TickerDetail {
  ticker: string;
  source: 'local_scanner';
  fetched_at: string;
  quote: {
    price: number;
    change_pct: number | null;
    quote_time: string;
    session: string;
  };
  analysis: TickerAnalysis | null;
  charts: { intraday: TickerBar[]; daily: TickerBar[] };
  pulls: TickerPull[];
  warnings: string[];
}

export interface ResearchResult {
  status: 'complete';
  provider: 'openrouter';
  model: string;
  answer: string;
  symbols: string[];
  generated_at: string;
}

export type Market = 'stocks' | 'memecoins' | 'sports';
export type MarketTab = 'pulse' | 'radar' | 'alpha';
export type CoinSort = 'volume' | 'market_cap' | 'gainers' | 'losers';

export interface CoinRow {
  id: string;
  symbol: string;
  name: string;
  price: number;
  price_label: string;
  change_24h: number | null;
  volume_label: string;
  market_cap_label: string;
  observed_at: string | null;
  stale: boolean;
}

export interface CoinCall {
  public_id: string;
  coin_id: string;
  symbol: string;
  name: string;
  caller_handle: string;
  status: 'active' | 'closed';
  entry_price_label: string;
  mark_price_label: string;
  mark_at: string | null;
  return_pct: number | null;
}

export interface CoinMarket {
  rows: CoinRow[];
  status: string;
  collected_at: string | null;
  refresh_failed: boolean;
  total: number;
  source: string;
}

export interface CoinDetail {
  coin: CoinRow;
  status: string;
  collected_at: string | null;
  refresh_failed: boolean;
  source: string;
  history: { observed_at: string; price: number }[];
  evidence: { observed_at: string | null; collected_at: string | null };
  in_current_snapshot: boolean;
  calls: CoinCall[];
}

export interface SportsRow {
  id?: string;
  event_id?: string;
  company?: string;
  ticker?: string;
  league?: string;
  away_abbreviation?: string;
  home_abbreviation?: string;
  away_score?: number;
  home_score?: number;
  start_time?: string;
  status_detail?: string;
  model_winner_team_name?: string;
  model_winner_probability_pct?: number;
  market_probability_pct?: number;
  model_winner_projected_score_display?: string;
  model_winner_opponent_projected_score_display?: string;
  radar_label?: string;
  radar_detail?: string;
  pulse_label?: string;
  price_label?: string;
  odds_label?: string;
  active_calls?: number;
  total_calls?: number;
}

export interface SportsMarket {
  events?: SportsRow[];
  rows?: SportsRow[];
  updated_at?: string;
  source_status?: string;
  source_error?: string;
}

export class NodeClient {
  readonly baseUrl: string;
  readonly token: string;

  constructor(baseUrl: string, token = '') {
    const value = baseUrl.trim();
    if (!value) throw new Error('Scanner address is unavailable');
    let parsed: URL;
    try {
      parsed = new URL(value);
    } catch {
      throw new Error('Enter a valid scanner address');
    }
    const loopbackHttp = parsed.protocol === 'http:'
      && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost');
    if (parsed.protocol !== 'https:' && !loopbackHttp) {
      throw new Error('Remote scanner connections must use HTTPS');
    }
    this.baseUrl = parsed.href.replace(/\/$/, '');
    this.token = token.trim();
  }

  private async request<T>(path: string, init?: RequestInit, timeoutMs = 12_000): Promise<T> {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        credentials: 'same-origin',
        signal: controller.signal,
        headers: {
          'Content-Type': 'application/json',
          ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
          ...(init?.headers || {}),
        },
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail || `Scanner returned ${response.status}`);
      }
      return response.json() as Promise<T>;
    } catch (error) {
      if (controller.signal.aborted) throw new Error('Scanner request timed out');
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  node(): Promise<NodeStatus> {
    return this.request('/api/v1/node');
  }

  memecoins(query = '', sort: CoinSort = 'volume', view: 'pulse' | 'radar' = 'radar'): Promise<CoinMarket> {
    const params = new URLSearchParams({ q: query, sort, view });
    return this.request(`/api/v1/markets/memecoins?${params}`, undefined, 20_000);
  }

  memecoin(coinId: string): Promise<CoinDetail> {
    return this.request(`/api/v1/markets/memecoins/coins/${encodeURIComponent(coinId)}`, undefined, 20_000);
  }

  memecoinCalls(): Promise<{ calls: CoinCall[] }> {
    return this.request('/api/v1/markets/memecoins/calls', undefined, 20_000);
  }

  sports(tab: MarketTab): Promise<SportsMarket> {
    return this.request(`/api/v1/markets/sports/${tab}`, undefined, 20_000);
  }

  providers(): Promise<{ providers: ProviderStatus[] }> {
    return this.request('/api/v1/providers');
  }

  coverage(): Promise<CoveragePayload> {
    return this.request('/api/v1/coverage');
  }

  setProviderRoute(capability: string, providers: string[]): Promise<{
    capability: string;
    providers: string[];
  }> {
    return this.request(`/api/v1/routes/${encodeURIComponent(capability)}`, {
      method: 'PUT',
      body: JSON.stringify({ providers }),
    });
  }

  openRouter(): Promise<OpenRouterConnection> {
    return this.request('/api/v1/connections/openrouter');
  }

  startOpenRouter(): Promise<{ flow_id: string; authorization_url: string; expires_at: string }> {
    return this.request('/api/v1/connections/openrouter/start', { method: 'POST' });
  }

  openRouterFlow(flowId: string): Promise<{ status: string; detail?: string }> {
    return this.request(`/api/v1/connections/openrouter/flows/${encodeURIComponent(flowId)}`);
  }

  disconnectOpenRouter(): Promise<OpenRouterConnection & { removed: boolean }> {
    return this.request('/api/v1/connections/openrouter', { method: 'DELETE' });
  }

  connectOpenRouterKey(key: string): Promise<OpenRouterConnection> {
    return this.request('/api/v1/connections/openrouter', {
      method: 'PUT',
      body: JSON.stringify({ key }),
    });
  }

  connectProvider(provider: string, key: string): Promise<{ provider: string; status: string }> {
    return this.request(`/api/v1/connections/${encodeURIComponent(provider)}`, {
      method: 'PUT',
      body: JSON.stringify({ key }),
    });
  }

  disconnectProvider(provider: string): Promise<{ provider: string; status: string }> {
    return this.request(`/api/v1/connections/${encodeURIComponent(provider)}`, {
      method: 'DELETE',
    });
  }

  scans(): Promise<{ receipts: ScanResult[] }> {
    return this.request('/api/v1/scans');
  }

  sourceScans(): Promise<{ receipts: ScanResult[]; warnings: string[] }> {
    return this.request('/api/v1/source-scans', undefined, 30_000);
  }

  setRatiCloudEnabled(enabled: boolean): Promise<{ source: string; status: string }> {
    return this.request('/api/v1/sources/rati-cloud', {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
    });
  }

  addRemoteScanner(input: RemoteScannerInput): Promise<{
    id: string; name: string; url: string; status: string;
  }> {
    return this.request('/api/v1/connections/scanners', {
      method: 'POST',
      body: JSON.stringify(input),
    });
  }

  removeRemoteScanner(scannerId: string): Promise<{ id: string; status: string }> {
    return this.request(`/api/v1/connections/scanners/${encodeURIComponent(scannerId)}`, {
      method: 'DELETE',
    });
  }

  liveScan(): Promise<ScanResult> {
    return this.request('/api/v1/scans', {
      method: 'POST',
      body: JSON.stringify({ universe: 'penny', min_price: 0.2, max_price: 5, top_n: 20 }),
    }, 180_000);
  }

  ticker(ticker: string): Promise<TickerDetail> {
    return this.request(`/api/v1/tickers/${encodeURIComponent(ticker)}`, undefined, 30_000);
  }

  research(prompt: string): Promise<ResearchResult> {
    return this.request('/api/v1/research', {
      method: 'POST',
      body: JSON.stringify({ prompt }),
    }, 60_000);
  }
}

export function normalizeNodeUrl(value: string): string {
  return new NodeClient(value).baseUrl;
}
