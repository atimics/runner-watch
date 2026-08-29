export interface NodeCapabilities {
  background_service: string;
  community: string;
  research: string;
  sports: string;
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
}

export interface ProviderStatus {
  id: string;
  title: string;
  state: string;
  enabled: boolean;
  configured: boolean;
  configuration_kind: string;
  feeds: ProviderFeed[];
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
  source: 'sample' | 'live';
  finished_at: string;
  elapsed_seconds: number;
  rows: ScanRow[];
  warnings: string[];
}

export class NodeClient {
  readonly baseUrl: string;

  constructor(baseUrl: string) {
    const parsed = new URL(baseUrl);
    const loopbackHttp = parsed.protocol === 'http:'
      && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost');
    if (parsed.protocol !== 'https:' && !loopbackHttp) {
      throw new Error('Remote scanner connections must use HTTPS');
    }
    this.baseUrl = parsed.href.replace(/\/$/, '');
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as { detail?: string };
      throw new Error(body.detail || `Scanner returned ${response.status}`);
    }
    return response.json() as Promise<T>;
  }

  node(): Promise<NodeStatus> {
    return this.request('/api/v1/node');
  }

  providers(): Promise<{ providers: ProviderStatus[] }> {
    return this.request('/api/v1/providers');
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

  sampleScan(): Promise<ScanResult> {
    return this.request('/api/v1/scans', {
      method: 'POST',
      body: JSON.stringify({ source: 'sample', universe: 'starter', top_n: 20 }),
    });
  }
}

export function normalizeNodeUrl(value: string): string {
  return new NodeClient(value).baseUrl;
}
