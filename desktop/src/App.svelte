<script lang="ts">
  import { onMount } from 'svelte';

  import {
    NodeClient,
    type NodeStatus,
    type OpenRouterConnection,
    type ProviderStatus,
    type ScanResult,
  } from './lib/node';

  let nodeUrl = 'http://127.0.0.1:8787';
  let node: NodeStatus | null = null;
  let providers: ProviderStatus[] = [];
  let openrouter: OpenRouterConnection = { status: 'disconnected', provider: 'openrouter' };
  let scan: ScanResult | null = null;
  let message = 'Connect to a local scanner or RATi AI Cloud.';
  let connecting = false;
  let scanning = false;
  let runtime = { appVersion: 'web', platform: 'browser' };

  function client(): NodeClient {
    return new NodeClient(nodeUrl);
  }

  async function refresh(reportError = true): Promise<boolean> {
    connecting = true;
    try {
      const api = client();
      const [nextNode, nextProviders, nextOpenRouter] = await Promise.all([
        api.node(),
        api.providers(),
        api.openRouter(),
      ]);
      node = nextNode;
      providers = nextProviders.providers;
      openrouter = nextOpenRouter;
      localStorage.setItem('rati.nodeUrl', api.baseUrl);
      nodeUrl = api.baseUrl;
      message = `${nextNode.mode.replace('_', ' ')} scanner connected`;
      return true;
    } catch (error) {
      node = null;
      providers = [];
      if (reportError) {
        message = error instanceof Error ? error.message : 'Could not connect to scanner';
      }
      return false;
    } finally {
      connecting = false;
    }
  }

  async function openExternal(url: string) {
    if (window.ratiDesktop) {
      await window.ratiDesktop.openExternal(url);
    } else {
      window.open(url, '_blank', 'noopener,noreferrer');
    }
  }

  async function connectOpenRouter() {
    try {
      const api = client();
      const flow = await api.startOpenRouter();
      await openExternal(flow.authorization_url);
      message = 'Finish connecting in your browser.';
      const deadline = Date.parse(flow.expires_at);
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        const status = await api.openRouterFlow(flow.flow_id);
        if (status.status === 'connected') {
          openrouter = await api.openRouter();
          message = 'OpenRouter connected. The key stays with this scanner.';
          return;
        }
        if (status.status === 'failed' || status.status === 'expired') {
          throw new Error(status.detail || `OpenRouter connection ${status.status}`);
        }
      }
      throw new Error('OpenRouter connection expired');
    } catch (error) {
      message = error instanceof Error ? error.message : 'OpenRouter connection failed';
    }
  }

  async function disconnectOpenRouter() {
    try {
      openrouter = await client().disconnectOpenRouter();
      message = openrouter.status === 'connected'
        ? 'The environment-managed OpenRouter connection is still active.'
        : 'OpenRouter disconnected from this scanner.';
    } catch (error) {
      message = error instanceof Error ? error.message : 'Could not disconnect OpenRouter';
    }
  }

  async function runSampleScan() {
    scanning = true;
    try {
      scan = await client().sampleScan();
      message = `Scan complete: ${scan.rows.length} ranked candidates.`;
    } catch (error) {
      message = error instanceof Error ? error.message : 'Scan failed';
    } finally {
      scanning = false;
    }
  }

  onMount(async () => {
    const saved = localStorage.getItem('rati.nodeUrl');
    if (window.ratiDesktop) {
      const desktop = await window.ratiDesktop.getRuntime();
      runtime = { appVersion: desktop.appVersion, platform: desktop.platform };
      nodeUrl = saved || desktop.nodeUrl;
    } else if (saved) {
      nodeUrl = saved;
    }
    const attempts = window.ratiDesktop ? 120 : 1;
    if (window.ratiDesktop) message = 'Starting the local scanner…';
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (await refresh(attempt === attempts - 1)) return;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  });
</script>

<svelte:head><title>RATi Desktop</title></svelte:head>

<div class="app-shell">
  <header>
    <div>
      <span class="eyebrow">RATi DESKTOP</span>
      <h1>Your scanner. Your sources.</h1>
    </div>
    <span class:online={node} class="connection-dot">{node ? 'Connected' : 'Disconnected'}</span>
  </header>

  <main>
    <section class="connection-panel">
      <label for="node-url">Scanner node</label>
      <div class="connection-row">
        <input id="node-url" bind:value={nodeUrl} spellcheck="false" />
        <button class="primary" onclick={() => refresh()} disabled={connecting}>
          {connecting ? 'Connecting…' : 'Connect'}
        </button>
      </div>
      <p class="status" aria-live="polite">{message}</p>
    </section>

    {#if node}
      <section class="node-summary">
        <div><small>Mode</small><strong>{node.mode.replace('_', ' ')}</strong></div>
        <div><small>Scanner</small><strong>{node.scanner_version}</strong></div>
        <div><small>Research</small><strong>{node.capabilities.research.replace('_', ' ')}</strong></div>
        <div><small>Community</small><strong>{node.capabilities.community}</strong></div>
      </section>

      <section class="workspace-grid">
        <article class="card action-card">
          <span class="eyebrow">LOCAL ENGINE</span>
          <h2>Run the scanner</h2>
          <p>Use deterministic sample data to verify that this app and scanner are separate and connected.</p>
          <button class="primary" onclick={runSampleScan} disabled={scanning || node.mode === 'cloud'}>
            {scanning ? 'Scanning…' : 'Run sample scan'}
          </button>
        </article>

        <article class="card action-card">
          <span class="eyebrow">OPTIONAL AI</span>
          <h2>OpenRouter</h2>
          <p>{openrouter.status === 'connected'
            ? `Connected · ${openrouter.key_fingerprint || openrouter.connection_method}`
            : 'Connect with OAuth PKCE. The desktop renderer never receives the generated key.'}</p>
          <div class="button-row">
            {#if openrouter.status === 'connected'}
              {#if openrouter.activity_url}
                <button onclick={() => openExternal(openrouter.activity_url!)}>View usage</button>
              {/if}
              <button onclick={disconnectOpenRouter}>Disconnect</button>
            {:else}
              <button class="primary" onclick={connectOpenRouter}>Connect OpenRouter</button>
            {/if}
          </div>
        </article>
      </section>

      {#if scan}
        <section class="scan-section">
          <div class="section-head">
            <div><span class="eyebrow">SCAN RECEIPT</span><h2>{scan.rows.length} candidates</h2></div>
            <small>{new Date(scan.finished_at).toLocaleString()} · {scan.elapsed_seconds.toFixed(2)}s</small>
          </div>
          <div class="scan-list">
            {#each scan.rows as row}
              <article class="scan-row">
                <div><strong>{row.ticker}</strong><small>{row.trade_state} · {row.rug_level} risk</small></div>
                <div><b>{row.score.toFixed(1)}</b><small>setup</small></div>
                <div><b>${row.price.toFixed(2)}</b><small>{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></div>
                <p>{row.state_reason}</p>
              </article>
            {/each}
          </div>
        </section>
      {/if}

      <section class="providers-section">
        <div class="section-head"><div><span class="eyebrow">SOURCE REGISTRY</span><h2>Providers</h2></div><small>{providers.length} connections</small></div>
        <div class="provider-grid">
          {#each providers as provider}
            <article class="provider-card">
              <div><strong>{provider.title}</strong><span class:ready={provider.state === 'ready' || provider.state === 'connected'}>{provider.state.replace('_', ' ')}</span></div>
              <p>{provider.feeds.map((feed) => feed.title).join(' · ')}</p>
              {#if provider.feeds[0]?.terms_url}
                <button class="text-button" onclick={() => openExternal(provider.feeds[0].terms_url!)}>Source terms ↗</button>
              {/if}
            </article>
          {/each}
        </div>
      </section>
    {:else}
      <section class="offline-library">
        <span class="eyebrow">LIBRARY MODE</span>
        <h2>No scanner connected</h2>
        <p>The desktop app remains available for saved receipts and settings, but it cannot create live scores, research, Calls, or alerts.</p>
      </section>
    {/if}
  </main>

  <footer>Desktop {runtime.appVersion} · {runtime.platform} · Node API v{node?.api_version || '—'}</footer>
</div>
