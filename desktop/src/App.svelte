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
  import {
    RATi_RUNNERS_URL,
    RunnersClient,
    type FlashRecord,
    type PulseData,
    type RadarData,
    type RunnerRow,
  } from './lib/runners';

  type View = 'pulse' | 'radar' | 'flash' | 'scanner' | 'settings';
  type ScannerMode = 'local' | 'cloud';

  let view: View = 'pulse';
  let scannerMode: ScannerMode = 'local';
  let nodeUrl = 'http://127.0.0.1:8787';
  let nodeToken = '';
  let localNodeUrl = '';
  let localNodeToken = '';
  let node: NodeStatus | null = null;
  let providers: ProviderStatus[] = [];
  let openrouter: OpenRouterConnection = { status: 'disconnected', provider: 'openrouter' };
  let scan: ScanResult | null = null;
  let receipts: ScanResult[] = [];
  let openrouterKey = '';
  let providerKeys: Record<string, string> = {};
  let researchPrompt = '';
  let research: ResearchResult | null = null;
  let scannerMessage = 'Starting the local scanner…';
  let connecting = false;
  let scanning = false;
  let researching = false;
  let isDesktop = false;
  let runtime = { appVersion: 'web', platform: 'browser' };

  let pulse: PulseData = { rows: [] };
  let radar: RadarData = { rows: [] };
  let flash: FlashRecord | null = null;
  let feedLoading = true;
  let feedMessage = 'Connecting to the RATi network…';
  let feedUpdatedAt = '';

  const navItems: { id: View; label: string; icon: string }[] = [
    { id: 'pulse', label: 'Pulse', icon: '◉' },
    { id: 'radar', label: 'Radar', icon: '⌁' },
    { id: 'flash', label: 'Flash', icon: 'ϟ' },
    { id: 'scanner', label: 'Scanner', icon: '⌕' },
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

  function writeCache(key: string, value: unknown) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // The live result remains available when local storage is full.
    }
  }

  function loadCachedReceipts(): ScanResult[] {
    const value = readCache<ScanResult[]>('rati.receipts', []);
    return Array.isArray(value) ? value.slice(0, 20) : [];
  }

  function rememberReceipts(next: ScanResult[]) {
    receipts = Array.from(new Map(next.map((receipt) => [receipt.id, receipt])).values())
      .sort((left, right) => Date.parse(right.finished_at) - Date.parse(left.finished_at))
      .slice(0, 20);
    writeCache('rati.receipts', receipts);
  }

  async function refreshFeeds() {
    feedLoading = true;
    const api = new RunnersClient();
    const results = await Promise.allSettled([api.pulse(), api.radar(), api.flash()]);
    let successes = 0;
    if (results[0].status === 'fulfilled') {
      pulse = results[0].value;
      writeCache('rati.feed.pulse', pulse);
      successes += 1;
    }
    if (results[1].status === 'fulfilled') {
      radar = results[1].value;
      writeCache('rati.feed.radar', radar);
      successes += 1;
    }
    if (results[2].status === 'fulfilled') {
      flash = results[2].value;
      writeCache('rati.feed.flash', flash);
      successes += 1;
    }
    feedLoading = false;
    if (successes) {
      feedUpdatedAt = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      feedMessage = successes === 3 ? 'Live from runners.rati.chat' : 'Partly live · saved data filled the gaps';
    } else {
      feedMessage = 'Offline · showing the latest saved data';
    }
  }

  async function refreshScanner(reportError = true): Promise<boolean> {
    connecting = true;
    try {
      const api = client();
      const [nextNode, nextProviders, nextOpenRouter] = await Promise.all([
        api.node(), api.providers(), api.openRouter(),
      ]);
      if (nextNode.api_version !== '1') {
        throw new Error(`This app needs Scanner API v1, but this scanner reported v${nextNode.api_version}`);
      }
      if (nextNode.mode !== 'cloud') {
        const history = await api.scans();
        rememberReceipts([...history.receipts, ...receipts]);
      }
      node = nextNode;
      providers = nextProviders.providers;
      openrouter = nextOpenRouter;
      nodeUrl = api.baseUrl;
      if (scannerMode === 'local') localStorage.setItem('rati.nodeUrl', api.baseUrl);
      sessionStorage.setItem('rati.nodeToken', nodeToken);
      scannerMessage = `${nextNode.mode.replace('_', ' ')} scanner connected`;
      return true;
    } catch (error) {
      node = null;
      providers = [];
      openrouter = { status: 'disconnected', provider: 'openrouter' };
      if (reportError) scannerMessage = error instanceof Error ? error.message : 'Could not connect to scanner';
      return false;
    } finally {
      connecting = false;
    }
  }

  async function chooseScannerMode(mode: ScannerMode) {
    scannerMode = mode;
    localStorage.setItem('rati.scannerMode', mode);
    if (mode === 'cloud') {
      nodeUrl = RATi_RUNNERS_URL;
      nodeToken = '';
      scannerMessage = 'Connecting to RATi Cloud…';
    } else {
      nodeUrl = localNodeUrl || localStorage.getItem('rati.nodeUrl') || 'http://127.0.0.1:8787';
      nodeToken = localNodeToken || sessionStorage.getItem('rati.nodeToken') || '';
      scannerMessage = 'Connecting to your local scanner…';
    }
    await refreshScanner();
  }

  async function openExternal(url: string) {
    if (window.ratiDesktop) await window.ratiDesktop.openExternal(url);
    else window.open(url, '_blank', 'noopener,noreferrer');
  }

  function openRunner(path: string) {
    void openExternal(new URL(path, RATi_RUNNERS_URL).href);
  }

  async function connectOpenRouter() {
    try {
      const api = client();
      const flow = await api.startOpenRouter();
      await openExternal(flow.authorization_url);
      scannerMessage = 'Finish connecting in your browser.';
      const deadline = Date.parse(flow.expires_at);
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 1200));
        const status = await api.openRouterFlow(flow.flow_id);
        if (status.status === 'connected') {
          openrouter = await api.openRouter();
          scannerMessage = 'OpenRouter connected. The key stays with this scanner.';
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

  async function disconnectOpenRouter() {
    try {
      openrouter = await client().disconnectOpenRouter();
      scannerMessage = openrouter.status === 'connected'
        ? 'The cloud-managed OpenRouter connection is still active.'
        : 'OpenRouter disconnected from this scanner.';
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Could not disconnect OpenRouter';
    }
  }

  async function connectOpenRouterKey() {
    try {
      openrouter = await client().connectOpenRouterKey(openrouterKey);
      openrouterKey = '';
      scannerMessage = 'OpenRouter connected. The key is stored by this scanner.';
      await refreshScanner(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Could not save the OpenRouter key';
    }
  }

  async function connectProvider(provider: ProviderStatus) {
    const key = providerKeys[provider.id] || '';
    try {
      await client().connectProvider(provider.id, key);
      providerKeys = { ...providerKeys, [provider.id]: '' };
      scannerMessage = `${provider.title} connected to this scanner.`;
      await refreshScanner(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : `Could not connect ${provider.title}`;
    }
  }

  async function disconnectProvider(provider: ProviderStatus) {
    try {
      await client().disconnectProvider(provider.id);
      scannerMessage = `${provider.title} disconnected from this scanner.`;
      await refreshScanner(false);
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : `Could not disconnect ${provider.title}`;
    }
  }

  async function runSampleScan() {
    scanning = true;
    try {
      scan = await client().sampleScan();
      rememberReceipts([scan, ...receipts]);
      scannerMessage = `Scan complete: ${scan.rows.length} ranked candidates.`;
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Scan failed';
    } finally {
      scanning = false;
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

  function changeText(row: RunnerRow): string {
    const value = Number(row.change_pct || 0);
    return `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`;
  }

  function confidenceText(value?: number): string {
    if (value == null) return '';
    return value <= 1 ? `${Math.round(value * 100)}% confidence` : `${Math.round(value)}% confidence`;
  }

  onMount(async () => {
    receipts = loadCachedReceipts();
    pulse = readCache<PulseData>('rati.feed.pulse', { rows: [] });
    radar = readCache<RadarData>('rati.feed.radar', { rows: [] });
    flash = readCache<FlashRecord | null>('rati.feed.flash', null);
    void refreshFeeds();

    const savedMode = localStorage.getItem('rati.scannerMode');
    scannerMode = savedMode === 'cloud' ? 'cloud' : 'local';
    if (window.ratiDesktop) {
      isDesktop = true;
      const desktop = await window.ratiDesktop.getRuntime();
      runtime = { appVersion: desktop.appVersion, platform: desktop.platform };
      localNodeUrl = desktop.nodeUrl || localStorage.getItem('rati.nodeUrl') || '';
      localNodeToken = desktop.nodeToken;
      if (desktop.scannerError) scannerMessage = desktop.scannerError;
    } else {
      localNodeUrl = localStorage.getItem('rati.nodeUrl') || window.location.origin;
      localNodeToken = sessionStorage.getItem('rati.nodeToken') || '';
    }
    if (scannerMode === 'cloud') {
      nodeUrl = RATi_RUNNERS_URL;
      nodeToken = '';
      await refreshScanner();
      return;
    }
    nodeUrl = localNodeUrl;
    nodeToken = localNodeToken;
    if (!nodeUrl) return;
    const attempts = isDesktop ? 120 : 1;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (await refreshScanner(attempt === attempts - 1)) return;
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  });
</script>

<svelte:head><title>RATi Runners</title></svelte:head>

<div class="app-shell">
  <header class="app-header">
    <button class="brand" onclick={() => view = 'pulse'} aria-label="Open Pulse">
      <span class="brand-mark">R</span><span><b>RATi</b><small>RUNNERS</small></span>
    </button>
    <nav aria-label="Main navigation">
      {#each navItems as item}
        <button class:active={view === item.id} onclick={() => view = item.id}><span>{item.icon}</span>{item.label}</button>
      {/each}
    </nav>
    <div class="header-actions">
      <div class="mode-control" aria-label="Scanner location">
        <button class:active={scannerMode === 'local'} onclick={() => chooseScannerMode('local')}>Local</button>
        <button class:active={scannerMode === 'cloud'} onclick={() => chooseScannerMode('cloud')}>Cloud</button>
      </div>
      <button class:active={view === 'settings'} class="settings-button" onclick={() => view = 'settings'} aria-label="Open settings">⚙</button>
    </div>
  </header>

  <main>
    {#if view === 'pulse'}
      <section class="screen-head">
        <div><span class="eyebrow">RATi RUNNERS</span><h1>Pulse</h1><p>Verified movement across the market.</p></div>
        <div class="live-state"><i></i><span>{feedMessage}</span>{#if feedUpdatedAt}<small>Updated {feedUpdatedAt}</small>{/if}</div>
      </section>
      {#if pulse.flash_record}
        <button class="flash-strip" onclick={() => view = 'flash'}>
          <span class="flash-avatar">ϟ</span><span><b>{pulse.flash_record.label}</b><small>{pulse.flash_record.model_label} · permanent scorecard</small></span>
          <strong>{pulse.flash_record.headline_rate_visible && pulse.flash_record.hit_rate != null ? `${Math.round(pulse.flash_record.hit_rate * 100)}% hit` : `${pulse.flash_record.settled} settled`}</strong>
          <span>{pulse.flash_record.hits} hits · {pulse.flash_record.misses} misses <b>View record ›</b></span>
        </button>
      {/if}
      <section class="runner-list" aria-busy={feedLoading}>
        {#each pulse.rows as row}
          <button class="runner-row" onclick={() => openRunner(`/t/${encodeURIComponent(row.ticker)}`)}>
            <span class="ticker-badge">{row.coin_label || row.ticker.slice(0, 3)}</span>
            <span class="runner-name"><strong>{row.ticker}</strong><small>{row.company || row.name || 'Market runner'}</small></span>
            <span class="runner-thesis"><b>{row.pulse_label || row.trade_state || 'Moving'}</b><small>{row.directional_thesis || row.case_thesis || row.case_source_name || 'Fresh verified movement'}</small></span>
            <span class="runner-risk"><small>{row.rug_level ? `${row.rug_level} risk` : row.social_label || row.source || 'public'}</small>{#if row.case_confidence != null}<b>{confidenceText(row.case_confidence)}</b>{/if}</span>
            <span class="runner-price"><strong>{row.price != null ? `$${row.price.toFixed(2)}` : '—'}</strong><small class:positive={Number(row.change_pct || 0) >= 0}>{changeText(row)}</small></span><span class="row-arrow">›</span>
          </button>
        {:else}
          <div class="empty-state"><span>◉</span><h2>No saved Pulse yet</h2><p>Connect to the internet once to load the public RATi feed. It will be saved for offline reading.</p></div>
        {/each}
      </section>
    {:else if view === 'radar'}
      <section class="screen-head"><div><span class="eyebrow">EVENT WATCH</span><h1>Radar</h1><p>{radar.rows.length} recent events from Pulse.</p></div><div class="live-state"><i></i><span>{feedMessage}</span></div></section>
      <section class="runner-list radar-list" aria-busy={feedLoading}>
        {#each radar.rows as row}
          <button class:updated={row.has_update} class="runner-row" onclick={() => openRunner(`/t/${encodeURIComponent(row.ticker)}`)}>
            <span class="radar-sweep"></span><span class="runner-name"><strong>{row.ticker}</strong><small>{row.company || row.name || 'Market runner'}</small></span>
            <span class="runner-thesis"><b>{row.pulse_label || 'New event'}</b><small>{row.directional_thesis || row.case_thesis || row.case_source_name || 'Fresh public evidence'}</small></span>
            <span class="event-count"><b>{row.event_count || 1}</b><small>events</small></span>
            <span class="runner-price"><strong>{row.price != null ? `$${row.price.toFixed(2)}` : '—'}</strong><small class:positive={Number(row.change_pct || 0) >= 0}>{changeText(row)}</small></span><span class="row-arrow">›</span>
          </button>
        {:else}<div class="empty-state"><span>⌁</span><h2>Radar is quiet</h2><p>Fresh news, filings, and market alerts will appear here.</p></div>{/each}
      </section>
    {:else if view === 'flash'}
      <section class="screen-head flash-heading"><div><span class="eyebrow">FLASH ϟ</span><h1>Forecast record</h1><p>Frozen calls. Later prices. No rewrites.</p></div><button onclick={() => openRunner('/flash/record')}>Open public record ↗</button></section>
      {#if flash?.current_version}
        <section class="record-hero">
          <div><span class="flash-avatar">ϟ</span><p><strong>{flash.current_version.label}</strong><small>{flash.current_version.model_label} · {flash.current_version.state.replace('_', ' ')}</small></p></div>
          <b>{flash.current_version.headline_rate_visible && flash.current_version.hit_rate != null ? `${Math.round(flash.current_version.hit_rate * 100)}%` : flash.current_version.settled}<small>{flash.current_version.headline_rate_visible ? 'hit rate' : 'settled'}</small></b>
          <p>{flash.current_version.hits} hits · {flash.current_version.misses} misses · {flash.current_version.no_calls} no calls · {flash.current_version.pending} pending</p><p>{flash.current_version.distinct_tickers} tickers · {flash.current_version.distinct_trading_days || 0} trading days</p>
        </section>
        <section class="record-grid">
          <article class="method-card"><span class="eyebrow">THE CONTRACT</span><h2>One fixed finish line</h2><p>An up or down call is judged at the next regular-session close. It is a hit only after a move larger than {flash.contract.minimum_move_pct.toFixed(1)}% in Flash's direction.</p><small>The headline rate appears after {flash.contract.headline_sample} settled forecasts.</small></article>
          <article class="version-card"><span class="eyebrow">VERSIONS</span><h2>Permanent scorecards</h2>{#each flash.versions as version}<div><span><b>{version.label}</b><small>{version.model_label} · {version.state}</small></span><strong>{version.settled} settled</strong></div>{/each}</article>
        </section>
        <section class="ledger"><div class="section-head"><div><span class="eyebrow">RECEIPTS</span><h2>Latest public results</h2></div></div>
          {#each flash.recent_results as result}<button onclick={() => openRunner(result.report_url)}><strong>{result.ticker}</strong><span>{result.direction.replace('_', ' ')} · {Math.round(result.probability_up * 100)}% up</span><b class:hit={result.classification === 'hit'} class:miss={result.classification === 'miss'}>{(result.classification || result.status).replace('_', ' ')}</b><small>{result.return_pct != null ? `${result.return_pct >= 0 ? '+' : ''}${result.return_pct.toFixed(1)}% · ` : ''}{result.version_label}</small></button>{:else}<p class="empty-copy">No public forecast receipts yet.</p>{/each}
        </section>
      {:else}<div class="empty-state"><span>ϟ</span><h2>No saved Flash record</h2><p>Connect once to load the public, permanent scorecard.</p></div>{/if}
    {:else if view === 'scanner'}
      <section class="screen-head"><div><span class="eyebrow">{scannerMode.toUpperCase()} ENGINE</span><h1>Scanner</h1><p>Run locally on this device or use RATi Cloud.</p></div><span class:online={node} class="connection-dot">{node ? 'Connected' : 'Disconnected'}</span></section>
      <section class="scanner-hero"><div><span class="eyebrow">SCANNER LOCATION</span><h2>{scannerMode === 'local' ? 'Private and on-device' : 'Managed by RATi Cloud'}</h2><p>{scannerMode === 'local' ? 'Your keys and scans stay with this scanner. Internet sources are optional.' : 'Connects to runners.rati.chat. No local setup or maintenance.'}</p></div><div class="large-mode-control"><button class:active={scannerMode === 'local'} onclick={() => chooseScannerMode('local')}><b>Local</b><small>This device</small></button><button class:active={scannerMode === 'cloud'} onclick={() => chooseScannerMode('cloud')}><b>Cloud</b><small>runners.rati.chat</small></button></div></section>
      <section class="connection-panel"><label for="node-url">Scanner address</label><div class="connection-row"><input id="node-url" bind:value={nodeUrl} spellcheck="false" /><button class="primary" onclick={() => refreshScanner()} disabled={connecting}>{connecting ? 'Connecting…' : 'Connect'}</button></div>{#if !isDesktop || scannerMode === 'local'}<label for="node-token">Access token <small>Only needed for a separate self-hosted scanner</small></label><input id="node-token" type="password" bind:value={nodeToken} autocomplete="off" />{/if}<p class="status" aria-live="polite">{scannerMessage}</p></section>
      {#if node}
        <section class="node-summary"><div><small>Mode</small><strong>{node.mode.replace('_', ' ')}</strong></div><div><small>Scanner</small><strong>{node.scanner_version}</strong></div><div><small>Research</small><strong>{node.capabilities.research.replace('_', ' ')}</strong></div><div><small>Sources</small><strong>{providers.filter((provider) => provider.configured || provider.state === 'ready').length} ready</strong></div></section>
        <section class="scan-action"><div><span class="eyebrow">RUN NOW</span><h2>Rank the starter universe</h2><p>The first run uses deterministic sample data. Add your own market sources in Settings when you are ready.</p></div><button class="primary" onclick={runSampleScan} disabled={scanning || node.mode === 'cloud'}>{scanning ? 'Scanning…' : node.mode === 'cloud' ? 'Managed by cloud' : 'Run sample scan'}</button></section>
      {:else}<section class="offline-library"><span class="eyebrow">SCANNER OFFLINE</span><h2>Your saved work is still here</h2><p>Pulse, Radar, Flash, and saved receipts continue to work. Connect a scanner only when you want a new local run.</p></section>{/if}
      {#if scan || receipts.length}<section class="scan-section"><div class="section-head"><div><span class="eyebrow">RECEIPT LIBRARY</span><h2>{receipts.length} saved scans</h2></div></div>{#if scan}<div class="scan-list">{#each scan.rows as row}<article class="scan-row"><div><strong>{row.ticker}</strong><small>{row.trade_state} · {row.rug_level} risk</small></div><div><b>{row.score.toFixed(1)}</b><small>setup</small></div><div><b>${row.price.toFixed(2)}</b><small>{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></div><p>{row.state_reason}</p></article>{/each}</div>{/if}<div class="receipt-grid">{#each receipts as receipt}<button class="receipt-card" onclick={() => scan = receipt}><strong>{receipt.rows.length} candidates</strong><small>{new Date(receipt.finished_at).toLocaleString()} · {receipt.source}</small></button>{/each}</div></section>{/if}
    {:else}
      <section class="screen-head"><div><span class="eyebrow">SCANNER SETTINGS</span><h1>Connections</h1><p>Add your own keys to the selected scanner.</p></div><span class:online={node} class="connection-dot">{scannerMode} · {node ? 'connected' : 'offline'}</span></section>
      {#if node}
        <section class="workspace-grid">
          <article class="card action-card"><span class="eyebrow">OPTIONAL AI</span><h2>OpenRouter</h2><p>{openrouter.status === 'connected' ? `Connected · ${openrouter.connection_method}` : 'Connect with OAuth PKCE or paste your own key.'}</p><div class="button-row">{#if node.mode === 'cloud'}<span>Managed by RATi Cloud</span>{:else if openrouter.status === 'connected'}{#if openrouter.activity_url}<button onclick={() => openExternal(openrouter.activity_url!)}>View usage</button>{/if}<button onclick={disconnectOpenRouter}>Disconnect</button>{:else}<button class="primary" onclick={connectOpenRouter}>Connect OpenRouter</button>{/if}</div>{#if node.mode !== 'cloud' && openrouter.status !== 'connected'}<div class="key-entry"><input type="password" bind:value={openrouterKey} autocomplete="off" placeholder="Or paste an sk-or- API key" aria-label="OpenRouter API key" /><button onclick={connectOpenRouterKey} disabled={openrouterKey.trim().length < 24}>Save key</button></div>{/if}</article>
          <article class="card"><span class="eyebrow">WHAT EACH MODE ADDS</span><h2>Local or Cloud</h2><p><b>Local</b> runs the open-source scanner on this device with your keys. <b>Cloud</b> runs the same app and scanner as a managed service at runners.rati.chat.</p></article>
        </section>
        {#if openrouter.status === 'connected' && node.mode !== 'cloud'}<section class="research-section"><div class="section-head"><div><span class="eyebrow">OPTIONAL AI</span><h2>Research</h2></div></div><textarea bind:value={researchPrompt} maxlength="6000" placeholder="What should RATi research?"></textarea><button class="primary" onclick={runResearch} disabled={researching || researchPrompt.trim().length < 3}>{researching ? 'Researching…' : 'Run research'}</button>{#if research}<article class="research-answer"><small>{research.model} · {new Date(research.generated_at).toLocaleString()}</small><p>{research.answer}</p></article>{/if}</section>{/if}
        <section class="providers-section"><div class="section-head"><div><span class="eyebrow">SOURCE REGISTRY</span><h2>Data sources</h2></div><small>{providers.length} connections</small></div><div class="provider-grid">{#each providers as provider}<article class="provider-card"><div><strong>{provider.title}</strong><span class:ready={provider.state === 'ready' || provider.state === 'connected'}>{provider.state.replace('_', ' ')}</span></div><p>{provider.feeds.map((feed) => feed.title).join(' · ')}</p>{#if provider.feeds[0]?.terms_url}<button class="text-button" onclick={() => openExternal(provider.feeds[0].terms_url!)}>Source terms ↗</button>{/if}{#if node.mode !== 'cloud' && provider.configuration_kind === 'api_key'}{#if provider.configured}<button onclick={() => disconnectProvider(provider)}>Disconnect</button>{:else}<div class="key-entry"><input type="password" value={providerKeys[provider.id] || ''} oninput={(event) => providerKeys = { ...providerKeys, [provider.id]: event.currentTarget.value }} autocomplete="off" placeholder={`${provider.title} API key`} aria-label={`${provider.title} API key`} /><button onclick={() => connectProvider(provider)} disabled={(providerKeys[provider.id] || '').trim().length < 8}>Save key</button></div>{/if}{/if}</article>{/each}</div></section>
      {:else}<section class="offline-library"><span class="eyebrow">SCANNER OFFLINE</span><h2>Connect a scanner first</h2><p>Choose Local or Cloud above, then open Scanner to connect.</p></section>{/if}
    {/if}
  </main>

  <footer>RATi Runners {runtime.appVersion} · {runtime.platform} · Scanner API v{node?.api_version || '—'} · Copyright © 2026 RATi contributors · AGPL-3.0-only · <button class="footer-link" onclick={() => openExternal('https://github.com/atimics/runner-watch')}>Source</button></footer>
</div>
