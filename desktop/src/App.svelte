<script lang="ts">
  import { onMount } from 'svelte';

  import {
    NodeClient,
    type NodeStatus,
    type OpenRouterConnection,
    type ProviderStatus,
    type ResearchResult,
    type ScanResult,
  } from './lib/node';

  let nodeUrl = 'http://127.0.0.1:8787';
  let nodeToken = '';
  let node: NodeStatus | null = null;
  let providers: ProviderStatus[] = [];
  let openrouter: OpenRouterConnection = { status: 'disconnected', provider: 'openrouter' };
  let scan: ScanResult | null = null;
  let receipts: ScanResult[] = [];
  let openrouterKey = '';
  let providerKeys: Record<string, string> = {};
  let researchPrompt = '';
  let research: ResearchResult | null = null;
  let message = 'Connect to a local scanner or RATi AI Cloud.';
  let connecting = false;
  let scanning = false;
  let researching = false;
  let isDesktop = false;
  let runtime = { appVersion: 'web', platform: 'browser' };

  function client(): NodeClient {
    return new NodeClient(nodeUrl, nodeToken);
  }

  function loadCachedReceipts(): ScanResult[] {
    try {
      const value = JSON.parse(localStorage.getItem('rati.receipts') || '[]');
      return Array.isArray(value) ? value.slice(0, 20) : [];
    } catch {
      return [];
    }
  }

  function rememberReceipts(next: ScanResult[]) {
    receipts = Array.from(new Map(next.map((receipt) => [receipt.id, receipt])).values())
      .sort((left, right) => Date.parse(right.finished_at) - Date.parse(left.finished_at))
      .slice(0, 20);
    try {
      localStorage.setItem('rati.receipts', JSON.stringify(receipts));
    } catch {
      message = 'Receipt history is visible now, but browser storage is full.';
    }
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
      if (nextNode.api_version !== '1') {
        throw new Error(`This app needs Node API v1, but the scanner reported v${nextNode.api_version}`);
      }
      if (nextNode.mode !== 'cloud') {
        const history = await api.scans();
        rememberReceipts([...history.receipts, ...receipts]);
      }
      node = nextNode;
      providers = nextProviders.providers;
      openrouter = nextOpenRouter;
      localStorage.setItem('rati.nodeUrl', api.baseUrl);
      sessionStorage.setItem('rati.nodeToken', nodeToken);
      nodeUrl = api.baseUrl;
      message = `${nextNode.mode.replace('_', ' ')} scanner connected`;
      return true;
    } catch (error) {
      node = null;
      providers = [];
      openrouter = { status: 'disconnected', provider: 'openrouter' };
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

  async function connectOpenRouterKey() {
    try {
      openrouter = await client().connectOpenRouterKey(openrouterKey);
      openrouterKey = '';
      message = 'OpenRouter connected. The key is stored by this scanner.';
      await refresh(false);
    } catch (error) {
      message = error instanceof Error ? error.message : 'Could not save the OpenRouter key';
    }
  }

  async function connectProvider(provider: ProviderStatus) {
    const key = providerKeys[provider.id] || '';
    try {
      await client().connectProvider(provider.id, key);
      providerKeys = { ...providerKeys, [provider.id]: '' };
      message = `${provider.title} connected to this scanner.`;
      await refresh(false);
    } catch (error) {
      message = error instanceof Error ? error.message : `Could not connect ${provider.title}`;
    }
  }

  async function disconnectProvider(provider: ProviderStatus) {
    try {
      await client().disconnectProvider(provider.id);
      message = `${provider.title} disconnected from this scanner.`;
      await refresh(false);
    } catch (error) {
      message = error instanceof Error ? error.message : `Could not disconnect ${provider.title}`;
    }
  }

  async function runSampleScan() {
    scanning = true;
    try {
      scan = await client().sampleScan();
      rememberReceipts([scan, ...receipts]);
      message = `Scan complete: ${scan.rows.length} ranked candidates.`;
    } catch (error) {
      message = error instanceof Error ? error.message : 'Scan failed';
    } finally {
      scanning = false;
    }
  }

  async function runResearch() {
    researching = true;
    try {
      research = await client().research(researchPrompt);
      message = `Research complete with ${research.model}.`;
    } catch (error) {
      message = error instanceof Error ? error.message : 'Research failed';
    } finally {
      researching = false;
    }
  }

  onMount(async () => {
    receipts = loadCachedReceipts();
    const saved = localStorage.getItem('rati.nodeUrl');
    if (window.ratiDesktop) {
      isDesktop = true;
      const desktop = await window.ratiDesktop.getRuntime();
      runtime = { appVersion: desktop.appVersion, platform: desktop.platform };
      nodeUrl = desktop.nodeUrl || saved || '';
      nodeToken = desktop.nodeToken;
      if (desktop.scannerError) message = desktop.scannerError;
    } else {
      nodeUrl = saved || window.location.origin;
      nodeToken = sessionStorage.getItem('rati.nodeToken') || '';
    }
    if (!nodeUrl) return;
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
      {#if !isDesktop}
        <label for="node-token">Access token <small>Required by self-hosted scanners</small></label>
        <input id="node-token" type="password" bind:value={nodeToken} autocomplete="off" />
      {/if}
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
            ? `Connected · ${openrouter.connection_method}`
            : 'Connect with OAuth PKCE. The desktop renderer never receives the generated key.'}</p>
          <div class="button-row">
            {#if node.mode === 'cloud'}
              <span>Managed by RATi AI Cloud</span>
            {:else if openrouter.status === 'connected'}
              {#if openrouter.activity_url}
                <button onclick={() => openExternal(openrouter.activity_url!)}>View usage</button>
              {/if}
              <button onclick={disconnectOpenRouter}>Disconnect</button>
            {:else}
              <button class="primary" onclick={connectOpenRouter}>Connect OpenRouter</button>
            {/if}
          </div>
          {#if node.mode !== 'cloud' && openrouter.status !== 'connected'}
            <div class="key-entry">
              <input
                type="password"
                bind:value={openrouterKey}
                autocomplete="off"
                placeholder="Or paste an sk-or- API key"
                aria-label="OpenRouter API key"
              />
              <button onclick={connectOpenRouterKey} disabled={openrouterKey.trim().length < 24}>
                Save key
              </button>
            </div>
          {/if}
        </article>
      </section>

      {#if openrouter.status === 'connected' && node.mode !== 'cloud'}
        <section class="research-section">
          <div class="section-head"><div><span class="eyebrow">OPTIONAL AI</span><h2>Research</h2></div></div>
          <textarea bind:value={researchPrompt} maxlength="6000" placeholder="What should RATi research? Include symbols and the decision you are exploring."></textarea>
          <button class="primary" onclick={runResearch} disabled={researching || researchPrompt.trim().length < 3}>
            {researching ? 'Researching…' : 'Run research'}
          </button>
          {#if research}
            <article class="research-answer">
              <small>{research.model} · {new Date(research.generated_at).toLocaleString()}</small>
              <p>{research.answer}</p>
            </article>
          {/if}
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
              {#if node.mode !== 'cloud' && provider.configuration_kind === 'api_key'}
                {#if provider.configured}
                  <button onclick={() => disconnectProvider(provider)}>Disconnect</button>
                {:else}
                  <div class="key-entry">
                    <input
                      type="password"
                      value={providerKeys[provider.id] || ''}
                      oninput={(event) => providerKeys = {
                        ...providerKeys,
                        [provider.id]: event.currentTarget.value,
                      }}
                      autocomplete="off"
                      placeholder={`${provider.title} API key`}
                      aria-label={`${provider.title} API key`}
                    />
                    <button
                      onclick={() => connectProvider(provider)}
                      disabled={(providerKeys[provider.id] || '').trim().length < 8}
                    >Save key</button>
                  </div>
                {/if}
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

    {#if scan || receipts.length}
      <section class="scan-section">
        <div class="section-head">
          <div><span class="eyebrow">RECEIPT LIBRARY</span><h2>{receipts.length} saved scans</h2></div>
          {#if scan}<small>{new Date(scan.finished_at).toLocaleString()} · {scan.elapsed_seconds.toFixed(2)}s</small>{/if}
        </div>
        {#if scan}
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
        {/if}
        <div class="receipt-grid">
          {#each receipts as receipt}
            <button class="receipt-card" onclick={() => scan = receipt}>
              <strong>{receipt.rows.length} candidates</strong>
              <small>{new Date(receipt.finished_at).toLocaleString()} · {receipt.source}</small>
            </button>
          {/each}
        </div>
      </section>
    {/if}
  </main>

  <footer>
    Desktop {runtime.appVersion} · {runtime.platform} · Node API v{node?.api_version || '—'} ·
    Copyright © 2026 RATi contributors · AGPL-3.0-only · No warranty ·
    <button class="footer-link" onclick={() => openExternal('https://github.com/atimics/runner-watch')}>Source</button>
  </footer>
</div>
