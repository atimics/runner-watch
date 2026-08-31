<script lang="ts">
  import { onMount } from 'svelte';

  import {
    NodeClient,
    type NodeStatus,
    type OpenRouterConnection,
    type ProviderStatus,
    type ResearchResult,
    type ScanResult,
    type TickerBar,
    type TickerDetail,
  } from './lib/node';
  import {
    latestLocalScan,
    localScanNeedsRefresh,
    sourceColor,
    sourceLabel,
    sourcedRows,
    type SourcedScanRow,
  } from './lib/local-feed';

  type View = 'pulse' | 'radar' | 'flash' | 'scanner' | 'settings' | 'ticker';

  let view: View = 'pulse';
  let nodeUrl = 'http://127.0.0.1:8787';
  let nodeToken = '';
  let node: NodeStatus | null = null;
  let providers: ProviderStatus[] = [];
  let openrouter: OpenRouterConnection = { status: 'disconnected', provider: 'openrouter' };
  let receipts: ScanResult[] = [];
  let selectedReceipt: ScanResult | null = null;
  let scannerMessage = 'Starting the local source hub…';
  let sourceWarnings: string[] = [];
  let connecting = false;
  let scanning = false;
  let researching = false;
  let autoScanAttempted = false;
  let openrouterKey = '';
  let providerKeys: Record<string, string> = {};
  let researchPrompt = '';
  let research: ResearchResult | null = null;
  let remoteName = '';
  let remoteUrl = '';
  let remoteToken = '';
  let runtime = { appVersion: 'web', platform: 'browser' };

  let selectedTicker: SourcedScanRow | null = null;
  let tickerDetail: TickerDetail | null = null;
  let tickerBackView: View = 'pulse';
  let tickerChart: 'intraday' | 'daily' = 'intraday';
  let tickerLoading = false;
  let tickerError = '';

  const navItems: { id: View; label: string; icon: string }[] = [
    { id: 'pulse', label: 'Pulse', icon: '◉' },
    { id: 'radar', label: 'Radar', icon: '⌁' },
    { id: 'flash', label: 'Receipts', icon: 'ϟ' },
    { id: 'scanner', label: 'Scan', icon: '⌕' },
  ];

  function client(): NodeClient {
    return new NodeClient(nodeUrl, nodeToken);
  }

  function readCache<T>(key: string, fallback: T): T {
    try {
      const value = localStorage.getItem(key);
      return value ? JSON.parse(value) as T : fallback;
    } catch {
      return fallback;
    }
  }

  function builtInReceipt(receipt: ScanResult): ScanResult {
    return { ...receipt, source_id: 'built-in-scanner', source_name: 'Built-in scanner' };
  }

  function rememberReceipts(next: ScanResult[]) {
    receipts = Array.from(new Map(
      next.map((receipt) => [`${receipt.source_id || 'built-in-scanner'}:${receipt.id}`, receipt]),
    ).values())
      .sort((left, right) => Date.parse(right.finished_at) - Date.parse(left.finished_at))
      .slice(0, 60);
    const local = receipts.filter((receipt) => (receipt.source_id || 'built-in-scanner') === 'built-in-scanner');
    try {
      localStorage.setItem('rati.receipts', JSON.stringify(local.slice(0, 20)));
    } catch {
      // Results remain available for this session when local storage is full.
    }
    selectedReceipt = latestLocalScan(selectedReceipt, receipts);
  }

  function allRows(): SourcedScanRow[] {
    return sourcedRows(receipts);
  }

  function radarRows(): SourcedScanRow[] {
    return [...allRows()].sort((left, right) => {
      const volume = (right.relative_volume || 0) - (left.relative_volume || 0);
      return volume || Math.abs(right.change_pct) - Math.abs(left.change_pct) || right.score - left.score;
    });
  }

  function builtInLatest(): ScanResult | null {
    return latestLocalScan(
      null,
      receipts.filter((receipt) => (receipt.source_id || 'built-in-scanner') === 'built-in-scanner'),
    );
  }

  function receiptColor(receipt: ScanResult): string {
    return sourceColor(receipt.source_id || 'built-in-scanner');
  }

  function configuredSourceCount(): number {
    return providers.filter((provider) => provider.enabled && provider.configured && provider.id !== 'openrouter').length;
  }

  function freeSources(): ProviderStatus[] {
    return providers.filter((provider) => provider.id !== 'openrouter' && (
      provider.configuration_kind === 'none' || provider.configuration_kind === 'toggle'
    ) && (provider.enabled || provider.id === 'rati-cloud'));
  }

  function keySources(): ProviderStatus[] {
    return providers.filter((provider) => provider.configuration_kind === 'api_key');
  }

  function remoteSources(): ProviderStatus[] {
    return providers.filter((provider) => provider.configuration_kind === 'remote_scanner');
  }

  async function refreshSources(reportError = true): Promise<boolean> {
    connecting = true;
    try {
      const api = client();
      const [nextNode, providerResult, nextOpenRouter, localResult] = await Promise.all([
        api.node(), api.providers(), api.openRouter(), api.scans(),
      ]);
      if (nextNode.api_version !== '1') {
        throw new Error(`This app needs Source API v1, but the local hub reported v${nextNode.api_version}`);
      }
      node = nextNode;
      providers = providerResult.providers;
      openrouter = nextOpenRouter;
      const external = await api.sourceScans().catch((error: unknown) => ({
        receipts: [] as ScanResult[],
        warnings: [error instanceof Error ? error.message : 'Other scanner sources could not be read'],
      }));
      sourceWarnings = external.warnings;
      rememberReceipts([
        ...localResult.receipts.map(builtInReceipt),
        ...external.receipts,
      ]);
      scannerMessage = `${configuredSourceCount()} sources ready`;
      void ensureBuiltInScan();
      return true;
    } catch (error) {
      node = null;
      providers = [];
      openrouter = { status: 'disconnected', provider: 'openrouter' };
      if (reportError) scannerMessage = error instanceof Error ? error.message : 'The local source hub is unavailable';
      return false;
    } finally {
      connecting = false;
    }
  }

  async function runBuiltInScan() {
    if (!node) return;
    scanning = true;
    scannerMessage = builtInLatest() ? 'Refreshing the built-in scanner source…' : 'Running the built-in scanner source…';
    try {
      const scan = builtInReceipt(await client().liveScan());
      rememberReceipts([scan, ...receipts]);
      selectedReceipt = scan;
      scannerMessage = `Built-in scan complete · ${scan.rows.length} candidates`;
      await refreshSources(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'The built-in scan failed';
    } finally {
      scanning = false;
    }
  }

  async function ensureBuiltInScan() {
    if (scanning || autoScanAttempted || !localScanNeedsRefresh(builtInLatest())) return;
    autoScanAttempted = true;
    await runBuiltInScan();
  }

  async function toggleRatiCloud(provider: ProviderStatus) {
    try {
      await client().setRatiCloudEnabled(!provider.enabled);
      scannerMessage = provider.enabled ? 'RATi source disabled.' : 'RATi source enabled.';
      await refreshSources(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Could not change the RATi source';
    }
  }

  async function addRemoteScanner() {
    try {
      await client().addRemoteScanner({ name: remoteName, url: remoteUrl, token: remoteToken });
      scannerMessage = `${remoteName.trim()} added as a source.`;
      remoteName = '';
      remoteUrl = '';
      remoteToken = '';
      await refreshSources(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Could not add the remote scanner';
    }
  }

  async function removeRemoteScanner(provider: ProviderStatus) {
    try {
      await client().removeRemoteScanner(provider.id.replace(/^remote:/, ''));
      scannerMessage = `${provider.title} removed.`;
      await refreshSources(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : `Could not remove ${provider.title}`;
    }
  }

  async function connectProvider(provider: ProviderStatus) {
    try {
      await client().connectProvider(provider.id, providerKeys[provider.id] || '');
      providerKeys = { ...providerKeys, [provider.id]: '' };
      scannerMessage = `${provider.title} connected.`;
      await refreshSources(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : `Could not connect ${provider.title}`;
    }
  }

  async function disconnectProvider(provider: ProviderStatus) {
    try {
      await client().disconnectProvider(provider.id);
      scannerMessage = `${provider.title} disconnected.`;
      await refreshSources(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : `Could not disconnect ${provider.title}`;
    }
  }

  async function openExternal(url: string) {
    if (window.ratiDesktop) await window.ratiDesktop.openExternal(url);
    else window.open(url, '_blank', 'noopener,noreferrer');
  }

  async function connectOpenRouter() {
    try {
      const api = client();
      const flow = await api.startOpenRouter();
      await openExternal(flow.authorization_url);
      scannerMessage = 'Finish connecting OpenRouter in your browser.';
      const deadline = Date.parse(flow.expires_at);
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        const status = await api.openRouterFlow(flow.flow_id);
        if (status.status === 'connected') {
          openrouter = await api.openRouter();
          scannerMessage = 'OpenRouter connected.';
          return;
        }
        if (status.status === 'failed' || status.status === 'expired') {
          throw new Error(status.detail || `OpenRouter connection ${status.status}`);
        }
      }
      throw new Error('OpenRouter connection expired');
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'OpenRouter connection failed';
    }
  }

  async function connectOpenRouterKey() {
    try {
      openrouter = await client().connectOpenRouterKey(openrouterKey);
      openrouterKey = '';
      scannerMessage = 'OpenRouter key saved in the system credential vault.';
      await refreshSources(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Could not save the OpenRouter key';
    }
  }

  async function disconnectOpenRouter() {
    try {
      openrouter = await client().disconnectOpenRouter();
      scannerMessage = 'OpenRouter disconnected.';
      await refreshSources(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Could not disconnect OpenRouter';
    }
  }

  async function runResearch() {
    researching = true;
    try {
      research = await client().research(researchPrompt);
      scannerMessage = `Research complete with ${research.model}.`;
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Research failed';
    } finally {
      researching = false;
    }
  }

  async function openTicker(row: SourcedScanRow, backView: View = view) {
    selectedTicker = row;
    tickerBackView = backView;
    tickerDetail = null;
    tickerChart = 'intraday';
    tickerError = '';
    tickerLoading = true;
    view = 'ticker';
    try {
      tickerDetail = await client().ticker(row.ticker);
      if (tickerDetail.charts.intraday.length < 2 && tickerDetail.charts.daily.length > 1) tickerChart = 'daily';
    } catch (error) {
      tickerError = error instanceof Error ? error.message : 'Could not load this ticker';
    } finally {
      tickerLoading = false;
    }
  }

  function chartPoints(bars: TickerBar[]): string {
    const usable = bars.filter((bar) => Number.isFinite(bar.close));
    if (usable.length < 2) return '';
    const values = usable.map((bar) => bar.close);
    const minimum = Math.min(...values);
    const maximum = Math.max(...values);
    const spread = Math.max(maximum - minimum, maximum * 0.005, 0.01);
    return usable.map((bar, index) => {
      const x = 20 + (index / (usable.length - 1)) * 960;
      const y = 225 - ((bar.close - minimum) / spread) * 190;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
  }

  function selectedChartBars(): TickerBar[] {
    return tickerDetail?.charts[tickerChart] || [];
  }

  function currentViewLabel(): string {
    if (view === 'settings') return 'Sources';
    if (view === 'ticker') return selectedTicker?.ticker || 'Ticker';
    return navItems.find((item) => item.id === view)?.label || 'RATi Swarm';
  }

  onMount(async () => {
    receipts = readCache<ScanResult[]>('rati.receipts', []).map(builtInReceipt);
    selectedReceipt = latestLocalScan(null, receipts);
    if (window.ratiDesktop) {
      const desktop = await window.ratiDesktop.getRuntime();
      runtime = { appVersion: desktop.appVersion, platform: desktop.platform };
      nodeUrl = desktop.nodeUrl;
      nodeToken = desktop.nodeToken;
      if (desktop.scannerError) scannerMessage = desktop.scannerError;
    } else {
      nodeUrl = window.location.origin;
      nodeToken = sessionStorage.getItem('rati.nodeToken') || '';
    }
    if (!nodeUrl) return;
    const attempts = window.ratiDesktop ? 120 : 1;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (await refreshSources(attempt === attempts - 1)) return;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  });
</script>

<svelte:head><title>RATi Swarm</title></svelte:head>

<div class="app-shell">
  <aside class="app-sidebar">
    <button class="brand" onclick={() => view = 'pulse'} aria-label="Open Pulse"><span class="brand-mark">R</span><span><b>RATi</b><small>SWARM</small></span></button>
    <nav class="app-nav" aria-label="Main navigation"><small>Workspace</small>{#each navItems as item}<button class:active={view === item.id} onclick={() => view = item.id}><span>{item.icon}</span>{item.label}</button>{/each}</nav>
    <div class="sidebar-actions">
      <small>Sources</small>
      <button class:active={view === 'settings'} class="settings-button" onclick={() => view = 'settings'}><span>⚙</span> Manage sources</button>
      <div class:online={node} class="sidebar-status"><i></i><span>{node ? `${configuredSourceCount()} ready` : connecting ? 'Starting' : 'Unavailable'}</span></div>
    </div>
  </aside>

  <section class="app-workspace">
    <header class="app-toolbar"><div><small>Local workspace</small><strong>{currentViewLabel()}</strong></div><div class="toolbar-state"><span>Source hub</span><i class:online={node}></i></div></header>
    <main>
      {#if view === 'pulse'}
        <section class="screen-head local-feed-head"><div><span class="eyebrow">ALL ENABLED SOURCES</span><h1>Pulse</h1><p>Candidates from the newest receipt produced by each scanner source.</p></div><div class="local-head-actions"><div class="live-state"><i></i><span>{scannerMessage}</span><small>{allRows().length} source items</small></div><button class="primary" onclick={() => refreshSources()} disabled={connecting}>{connecting ? 'Refreshing…' : 'Refresh sources'}</button></div></section>
        <section class="runner-list">
          {#each allRows() as row}
            <button class="runner-row source-row" style={`--source-color:${row.source_color}`} onclick={() => openTicker(row, 'pulse')}>
              <span class="source-marker"></span><span class="ticker-badge">{row.ticker.slice(0, 3)}</span><span class="runner-name"><strong>{row.ticker}</strong><small>{row.source_name}</small></span><span class="runner-thesis"><b>{row.trade_state}</b><small>{row.state_reason}</small></span><span class="runner-risk"><small>{row.rug_level} risk</small><b>{row.score.toFixed(1)} setup</b></span><span class="runner-price"><strong>${row.price.toFixed(2)}</strong><small class:positive={row.change_pct >= 0}>{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></span><span class="row-arrow">›</span>
            </button>
          {:else}<div class="empty-state"><span>◉</span><h2>No source results yet</h2><p>The built-in scanner is ready without API keys. Run it now or enable another scanner source.</p><button class="primary" onclick={runBuiltInScan} disabled={!node || scanning}>{scanning ? 'Scanning…' : 'Run built-in scanner'}</button></div>{/each}
        </section>
      {:else if view === 'radar'}
        <section class="screen-head local-feed-head"><div><span class="eyebrow">SOURCE ACTIVITY</span><h1>Radar</h1><p>Results from every enabled scanner source, ranked by relative volume and movement.</p></div><button class="primary" onclick={() => refreshSources()} disabled={connecting}>{connecting ? 'Refreshing…' : 'Refresh'}</button></section>
        <section class="runner-list radar-list">{#each radarRows() as row}<button class="runner-row source-row" style={`--source-color:${row.source_color}`} onclick={() => openTicker(row, 'radar')}><span class="source-marker"></span><span class="radar-sweep"></span><span class="runner-name"><strong>{row.ticker}</strong><small>{row.source_name}</small></span><span class="runner-thesis"><b>{row.trade_state}</b><small>{row.state_reason}</small></span><span class="event-count"><b>{row.relative_volume == null ? '—' : `${row.relative_volume.toFixed(1)}×`}</b><small>rel volume</small></span><span class="runner-price"><strong>${row.price.toFixed(2)}</strong><small class:positive={row.change_pct >= 0}>{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></span><span class="row-arrow">›</span></button>{:else}<div class="empty-state"><span>⌁</span><h2>Radar is empty</h2><p>Run or enable a scanner source to add results.</p></div>{/each}</section>
      {:else if view === 'flash'}
        <section class="screen-head flash-heading local-feed-head"><div><span class="eyebrow">SOURCE RECEIPTS</span><h1>Receipts</h1><p>Recent scan output, grouped by the source that produced it.</p></div><button class="primary" onclick={() => refreshSources()} disabled={connecting}>Refresh sources</button></section>
        <section class="receipt-grid source-receipts">{#each receipts as receipt}<button class:active={receipt.id === selectedReceipt?.id} style={`--source-color:${receiptColor(receipt)}`} onclick={() => selectedReceipt = receipt}><span class="source-marker"></span><strong>{receipt.rows.length} candidates</strong><small>{sourceLabel(receipt)} · {new Date(receipt.finished_at).toLocaleString()}</small></button>{:else}<div class="empty-state"><span>ϟ</span><h2>No receipts yet</h2><p>Run the built-in scanner or enable another scanner source.</p></div>{/each}</section>
        {#if selectedReceipt}<section class="scan-section"><div class="section-head"><div><span class="eyebrow">{sourceLabel(selectedReceipt).toUpperCase()}</span><h2>Ranked results</h2></div><small>{selectedReceipt.elapsed_seconds.toFixed(1)} seconds</small></div><div class="scan-list">{#each sourcedRows([selectedReceipt]) as row}<button class="scan-row source-row" style={`--source-color:${row.source_color}`} onclick={() => openTicker(row, 'flash')}><span class="source-marker"></span><div><strong>{row.ticker}</strong><small>{row.trade_state} · {row.rug_level} risk</small></div><div><b>{row.score.toFixed(1)}</b><small>setup</small></div><div><b>${row.price.toFixed(2)}</b><small>{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></div><p>{row.state_reason}</p></button>{/each}</div></section>{/if}
      {:else if view === 'scanner'}
        <section class="screen-head"><div><span class="eyebrow">BUILT-IN SOURCE</span><h1>Scan</h1><p>The bundled scanner is one source in this workspace. It uses included free market data by default.</p></div><span class:online={node} class="connection-dot">{node ? 'Ready' : 'Unavailable'}</span></section>
        <section class="scanner-hero"><div><span class="eyebrow">NO ACCOUNT REQUIRED</span><h2>Built-in scanner</h2><p>Yahoo and other no-key sources are preconfigured. Add optional keys in Sources when you want extra coverage.</p></div><button class="primary" onclick={runBuiltInScan} disabled={scanning || !node}>{scanning ? 'Scanning…' : 'Run scanner'}</button></section>
        {#if builtInLatest()}<section class="node-summary"><div><small>Last run</small><strong>{new Date(builtInLatest()!.finished_at).toLocaleString()}</strong></div><div><small>Candidates</small><strong>{builtInLatest()!.rows.length}</strong></div><div><small>Symbols</small><strong>{builtInLatest()!.scanned_symbols || '—'}</strong></div><div><small>Time</small><strong>{builtInLatest()!.elapsed_seconds.toFixed(1)}s</strong></div></section>{/if}
        {#each sourceWarnings as warning}<p class="pull-warning">{warning}</p>{/each}
      {:else if view === 'ticker'}
        <section class="ticker-local-head"><button class="back-button" onclick={() => view = tickerBackView}>← Back to {tickerBackView}</button>{#if selectedTicker}<span class="source-badge" style={`--source-color:${selectedTicker.source_color}`}>{selectedTicker.source_name}</span>{/if}</section>
        {#if tickerLoading}<section class="ticker-loading"><span class="local-orbit">⌁</span><h1>{selectedTicker?.ticker}</h1><p>Pulling price bars from the configured market-data sources…</p></section>{:else if tickerError}<section class="offline-library ticker-error"><span class="eyebrow">TICKER</span><h2>Could not load {selectedTicker?.ticker}</h2><p>{tickerError}</p></section>{:else if tickerDetail}<section class="ticker-hero"><div><span class="eyebrow">SOURCE ANALYSIS</span><h1>{tickerDetail.ticker}</h1><p>Fresh analysis from the built-in market-data sources.</p></div><div class="ticker-quote"><strong>${tickerDetail.quote.price.toFixed(2)}</strong><b class:positive={Number(tickerDetail.quote.change_pct || 0) >= 0}>{tickerDetail.quote.change_pct == null ? '—' : `${tickerDetail.quote.change_pct >= 0 ? '+' : ''}${tickerDetail.quote.change_pct.toFixed(2)}%`}</b><small>{tickerDetail.quote.session} · {new Date(tickerDetail.quote.quote_time).toLocaleString()}</small></div></section><section class="ticker-chart-card"><div class="section-head"><div><span class="eyebrow">PRICE BARS</span><h2>{tickerChart === 'intraday' ? 'Five-minute chart' : 'Daily chart'}</h2></div><div class="chart-controls"><button class:active={tickerChart === 'intraday'} onclick={() => tickerChart = 'intraday'}>5 minute</button><button class:active={tickerChart === 'daily'} onclick={() => tickerChart = 'daily'}>Daily</button></div></div>{#if selectedChartBars().length > 1}<svg class="price-chart" viewBox="0 0 1000 250" role="img" aria-label={`${tickerDetail.ticker} chart`}><polyline points={chartPoints(selectedChartBars())}></polyline></svg>{:else}<p class="empty-copy">The source did not return enough bars for this chart.</p>{/if}</section>{#if tickerDetail.analysis}<section class="ticker-metrics"><div><small>Trade state</small><strong>{tickerDetail.analysis.trade_state}</strong></div><div><small>Setup score</small><strong>{tickerDetail.analysis.score.toFixed(1)}</strong></div><div><small>Rug risk</small><strong>{tickerDetail.analysis.rug_level}</strong></div><div><small>Relative volume</small><strong>{tickerDetail.analysis.relative_volume == null ? '—' : `${tickerDetail.analysis.relative_volume.toFixed(1)}×`}</strong></div></section>{/if}<section class="data-pulls"><div class="section-head"><div><span class="eyebrow">DATA PULLS</span><h2>Sources used</h2></div></div>{#each tickerDetail.pulls as pull}<article><span class:failed={pull.status === 'failed'} class="pull-status">{pull.status}</span><div><strong>{pull.label}</strong><small>{pull.provider} · {pull.feed}</small></div><b>{pull.bars} bars</b><small>{pull.fallback_used ? 'Fallback used' : 'Primary source'}</small></article>{/each}{#each tickerDetail.warnings as warning}<p class="pull-warning">{warning}</p>{/each}</section>{/if}
      {:else}
        <section class="screen-head"><div><span class="eyebrow">LOCAL SOURCE HUB</span><h1>Sources</h1><p>Free sources work immediately. Optional keys and remote scanner tokens stay in the system credential vault.</p></div><span class:online={node} class="connection-dot">{configuredSourceCount()} ready</span></section>
        <section class="providers-section"><div class="section-head"><div><span class="eyebrow">INCLUDED</span><h2>Free, no-key sources</h2></div><small>Ready by default</small></div><div class="provider-grid">{#each freeSources() as provider}<article class="provider-card" style={`--source-color:${sourceColor(provider.id)}`}><div><strong>{provider.title}</strong><span class:ready={provider.enabled}>{provider.enabled ? 'enabled' : 'available'}</span></div><p>{provider.feeds.map((feed) => feed.title).join(' · ')}</p>{#if provider.id === 'rati-cloud'}<button class={provider.enabled ? '' : 'primary'} onclick={() => toggleRatiCloud(provider)}>{provider.enabled ? 'Disable source' : 'Enable free source'}</button>{:else}<small class="included-label">No setup or API key</small>{/if}{#if provider.feeds[0]?.terms_url}<button class="text-button" onclick={() => openExternal(provider.feeds[0].terms_url!)}>Source terms ↗</button>{/if}</article>{/each}</div></section>
        <section class="providers-section"><div class="section-head"><div><span class="eyebrow">REMOTE SCANNERS</span><h2>Add any scanner</h2></div><small>HTTPS, or loopback HTTP</small></div><div class="remote-source-form"><input bind:value={remoteName} placeholder="Source name" aria-label="Remote scanner name" /><input bind:value={remoteUrl} placeholder="https://scanner.example.com" aria-label="Remote scanner address" /><input type="password" bind:value={remoteToken} placeholder="Access token, if required" aria-label="Remote scanner token" autocomplete="off" /><button class="primary" onclick={addRemoteScanner} disabled={!remoteName.trim() || !remoteUrl.trim()}>Add source</button></div>{#if remoteSources().length}<div class="provider-grid remote-grid">{#each remoteSources() as provider}<article class="provider-card remote-card" style={`--source-color:${sourceColor(provider.id)}`}><div><strong>{provider.title}</strong><span class="ready">connected</span></div><p>{provider.feeds[0]?.title}</p><button onclick={() => removeRemoteScanner(provider)}>Remove</button></article>{/each}</div>{/if}</section>
        <section class="providers-section"><div class="section-head"><div><span class="eyebrow">OPTIONAL</span><h2>API-key sources</h2></div><small>Add only what you use</small></div><div class="provider-grid">{#each keySources() as provider}<article class="provider-card"><div><strong>{provider.title}</strong><span class:ready={provider.configured}>{provider.configured ? 'connected' : 'optional'}</span></div><p>{provider.feeds.map((feed) => feed.title).join(' · ')}</p>{#if provider.configured}<button onclick={() => disconnectProvider(provider)}>Disconnect</button>{:else}<div class="key-entry"><input type="password" value={providerKeys[provider.id] || ''} oninput={(event) => providerKeys = { ...providerKeys, [provider.id]: event.currentTarget.value }} autocomplete="off" placeholder={`${provider.title} API key`} /><button onclick={() => connectProvider(provider)} disabled={(providerKeys[provider.id] || '').trim().length < 8}>Save key</button></div>{/if}{#if provider.feeds[0]?.terms_url}<button class="text-button" onclick={() => openExternal(provider.feeds[0].terms_url!)}>Source terms ↗</button>{/if}</article>{/each}</div></section>
        <section class="workspace-grid"><article class="card action-card"><span class="eyebrow">OPTIONAL AI SOURCE</span><h2>OpenRouter</h2><p>{openrouter.status === 'connected' ? 'Connected to this workspace.' : 'Connect with OAuth or paste your own key.'}</p>{#if openrouter.status === 'connected'}<button onclick={disconnectOpenRouter}>Disconnect</button>{:else}<div class="button-row"><button class="primary" onclick={connectOpenRouter}>Connect OpenRouter</button></div><div class="key-entry"><input type="password" bind:value={openrouterKey} autocomplete="off" placeholder="Or paste an sk-or- API key" /><button onclick={connectOpenRouterKey} disabled={openrouterKey.trim().length < 24}>Save key</button></div>{/if}</article></section>
        {#if openrouter.status === 'connected'}<section class="research-section"><div class="section-head"><div><span class="eyebrow">OPTIONAL AI</span><h2>Research</h2></div></div><textarea bind:value={researchPrompt} maxlength="6000" placeholder="What should RATi research?"></textarea><button class="primary" onclick={runResearch} disabled={researching || researchPrompt.trim().length < 3}>{researching ? 'Researching…' : 'Run research'}</button>{#if research}<article class="research-answer"><small>{research.model}</small><p>{research.answer}</p></article>{/if}</section>{/if}
      {/if}
    </main>
    <footer>RATi Swarm {runtime.appVersion} · {runtime.platform} · Source API v{node?.api_version || '—'} · AGPL-3.0-only · <button class="footer-link" onclick={() => openExternal('https://github.com/atimics/runner-watch')}>Source</button></footer>
  </section>
</div>
