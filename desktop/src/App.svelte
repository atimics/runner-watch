<script lang="ts">
  import { onMount } from 'svelte';

  import {
    NodeClient,
    type NodeStatus,
    type OpenRouterConnection,
    type ProviderStatus,
    type ResearchResult,
    type ScanResult,
    type ScanRow,
    type TickerBar,
    type TickerDetail,
  } from './lib/node';
  import {
    latestLocalScan,
    localRadarRows,
    localRunnerRow,
    localScanNeedsRefresh,
    shouldFetchCloudFeeds,
  } from './lib/local-feed';
  import {
    RATi_RUNNERS_URL,
    RunnersClient,
    type FlashRecord,
    type PulseData,
    type RadarData,
    type RunnerRow,
  } from './lib/runners';

  type View = 'pulse' | 'radar' | 'flash' | 'scanner' | 'settings' | 'ticker';
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
  let localFeedMessage = 'Waiting for the local scanner…';
  let cloudRequestId = 0;
  let scannerRequestId = 0;
  let autoScanAttempted = false;
  let selectedTicker: RunnerRow | null = null;
  let tickerDetail: TickerDetail | null = null;
  let tickerBackView: View = 'pulse';
  let tickerChart: 'intraday' | 'daily' = 'intraday';
  let tickerLoading = false;
  let tickerError = '';

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
    return Array.isArray(value) ? value.filter((receipt) => receipt.source === 'live').slice(0, 20) : [];
  }

  function rememberReceipts(next: ScanResult[]) {
    receipts = Array.from(new Map(
      next.filter((receipt) => receipt.source === 'live').map((receipt) => [receipt.id, receipt]),
    ).values())
      .sort((left, right) => Date.parse(right.finished_at) - Date.parse(left.finished_at))
      .slice(0, 20);
    writeCache('rati.receipts', receipts);
  }

  function activeLocalScan(): ScanResult | null {
    return scan || receipts[0] || null;
  }

  function localPulseRows(): ScanRow[] {
    return activeLocalScan()?.rows || [];
  }

  function localFeedTime(): string {
    const value = activeLocalScan()?.finished_at;
    return value ? new Date(value).toLocaleString() : '';
  }

  function loadCloudCache() {
    pulse = readCache<PulseData>('rati.feed.pulse', { rows: [] });
    radar = readCache<RadarData>('rati.feed.radar', { rows: [] });
    flash = readCache<FlashRecord | null>('rati.feed.flash', null);
  }

  function clearCloudState() {
    pulse = { rows: [] };
    radar = { rows: [] };
    flash = null;
    feedLoading = false;
    feedMessage = 'Cloud is off in Local mode.';
    feedUpdatedAt = '';
  }

  async function refreshFeeds() {
    if (!shouldFetchCloudFeeds(scannerMode)) return;
    const requestId = ++cloudRequestId;
    feedLoading = true;
    const api = new RunnersClient();
    const results = await Promise.allSettled([api.pulse(), api.radar(), api.flash()]);
    if (scannerMode !== 'cloud' || requestId !== cloudRequestId) return;
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
    const requestId = ++scannerRequestId;
    const expectedMode = scannerMode;
    connecting = true;
    try {
      const api = client();
      const [nextNode, nextProviders, nextOpenRouter] = await Promise.all([
        api.node(), api.providers(), api.openRouter(),
      ]);
      if (requestId !== scannerRequestId || expectedMode !== scannerMode) return false;
      if (nextNode.api_version !== '1') {
        throw new Error(`This app needs Scanner API v1, but this scanner reported v${nextNode.api_version}`);
      }
      if (expectedMode === 'local' && nextNode.mode === 'cloud') {
        throw new Error('Local mode cannot connect to a Cloud scanner. Choose Cloud to use runners.rati.chat.');
      }
      if (expectedMode === 'cloud' && nextNode.mode !== 'cloud') {
        throw new Error('Cloud mode requires the managed RATi Cloud scanner.');
      }
      if (nextNode.mode !== 'cloud') {
        const history = await api.scans();
        if (requestId !== scannerRequestId || expectedMode !== scannerMode) return false;
        rememberReceipts([...history.receipts, ...receipts]);
        scan = latestLocalScan(scan, receipts);
        if (scan) localFeedMessage = `Local receipt · ${new Date(scan.finished_at).toLocaleString()}`;
      }
      node = nextNode;
      providers = nextProviders.providers;
      openrouter = nextOpenRouter;
      nodeUrl = api.baseUrl;
      if (scannerMode === 'local') localStorage.setItem('rati.nodeUrl', api.baseUrl);
      sessionStorage.setItem('rati.nodeToken', nodeToken);
      scannerMessage = `${nextNode.mode.replace('_', ' ')} scanner connected`;
      if (scannerMode === 'local') void ensureLocalFeed();
      return true;
    } catch (error) {
      if (requestId !== scannerRequestId || expectedMode !== scannerMode) return false;
      node = null;
      providers = [];
      openrouter = { status: 'disconnected', provider: 'openrouter' };
      if (reportError) scannerMessage = error instanceof Error ? error.message : 'Could not connect to scanner';
      return false;
    } finally {
      if (requestId === scannerRequestId) connecting = false;
    }
  }

  async function chooseScannerMode(mode: ScannerMode) {
    cloudRequestId += 1;
    scannerRequestId += 1;
    scannerMode = mode;
    localStorage.setItem('rati.scannerMode', mode);
    if (mode === 'cloud') {
      loadCloudCache();
      void refreshFeeds();
      nodeUrl = RATi_RUNNERS_URL;
      nodeToken = '';
      scannerMessage = 'Connecting to RATi Cloud…';
    } else {
      clearCloudState();
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

  async function runLiveScan() {
    if (scannerMode !== 'local' || !node || node.mode === 'cloud') {
      localFeedMessage = 'Connect the local scanner before starting a scan.';
      return;
    }
    scanning = true;
    localFeedMessage = activeLocalScan()
      ? 'Refreshing local market data… showing the previous receipt for now.'
      : 'Scanning live market data on this device…';
    try {
      scan = await client().liveScan();
      rememberReceipts([scan, ...receipts]);
      scannerMessage = `Scan complete: ${scan.rows.length} ranked candidates.`;
      localFeedMessage = `Local scan complete · ${new Date(scan.finished_at).toLocaleString()}`;
    } catch (error) {
      scannerMessage = error instanceof Error ? error.message : 'Scan failed';
      localFeedMessage = activeLocalScan()
        ? `Local refresh failed · showing ${localFeedTime()}`
        : scannerMessage;
    } finally {
      scanning = false;
    }
  }

  async function ensureLocalFeed() {
    if (scannerMode !== 'local' || !node || node.mode === 'cloud' || scanning) return;
    const latest = activeLocalScan();
    if (!localScanNeedsRefresh(latest)) {
      scan = latest;
      localFeedMessage = `Local scan · ${localFeedTime()}`;
      return;
    }
    if (autoScanAttempted) return;
    autoScanAttempted = true;
    await runLiveScan();
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

  async function openTicker(row: RunnerRow, backView: View = view) {
    if (scannerMode === 'cloud') {
      await openRunner(`/t/${encodeURIComponent(row.ticker)}`);
      return;
    }
    selectedTicker = row;
    tickerBackView = ['pulse', 'radar', 'flash', 'scanner'].includes(backView) ? backView : 'pulse';
    tickerDetail = null;
    tickerChart = 'intraday';
    tickerError = '';
    tickerLoading = true;
    view = 'ticker';
    try {
      if (!node || node.mode === 'cloud') {
        throw new Error('The local scanner is not connected. Open Scanner and reconnect it.');
      }
      tickerDetail = await client().ticker(row.ticker);
      if (tickerDetail.charts.intraday.length < 2 && tickerDetail.charts.daily.length > 1) {
        tickerChart = 'daily';
      }
    } catch (error) {
      tickerError = error instanceof Error ? error.message : 'Could not load this ticker locally';
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

  function chartRange(bars: TickerBar[]): string {
    if (!bars.length) return 'No bars returned';
    const values = bars.map((bar) => bar.close);
    return `$${Math.min(...values).toFixed(2)} – $${Math.max(...values).toFixed(2)}`;
  }

  function selectedChartBars(): TickerBar[] {
    return tickerDetail?.charts[tickerChart] || [];
  }

  function compactNumber(value?: number | null): string {
    if (value == null || !Number.isFinite(value)) return '—';
    return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
  }

  onMount(async () => {
    view = 'pulse';
    receipts = loadCachedReceipts();
    scan = latestLocalScan(null, receipts);
    if (scan) localFeedMessage = `Saved local scan · ${new Date(scan.finished_at).toLocaleString()}`;
    const savedMode = localStorage.getItem('rati.scannerMode');
    scannerMode = savedMode === 'cloud' ? 'cloud' : 'local';
    if (scannerMode === 'cloud') {
      loadCloudCache();
      void refreshFeeds();
    } else {
      cloudRequestId += 1;
      clearCloudState();
    }
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

<svelte:head><title>RATi Swarm</title></svelte:head>

<div class="app-shell">
  <header class="app-header">
    <button class="brand" onclick={() => view = 'pulse'} aria-label="Open Pulse">
      <span class="brand-mark">R</span><span><b>RATi</b><small>SWARM</small></span>
    </button>
    <nav aria-label="Main navigation">
      {#each navItems as item}
        <button class:active={view === item.id} onclick={() => view = item.id}><span>{item.icon}</span>{item.label}</button>
      {/each}
    </nav>
    <div class="header-actions">
      <div class="mode-control" aria-label="Scanner location">
        <button class:active={scannerMode === 'local'} onclick={() => chooseScannerMode('local')}>Local</button>
        <button class="cloud-choice" class:active={scannerMode === 'cloud'} onclick={() => chooseScannerMode('cloud')}>Cloud</button>
      </div>
      <button class:active={view === 'settings'} class="settings-button" onclick={() => view = 'settings'} aria-label="Open settings">⚙</button>
    </div>
  </header>

  <main>
    {#if view === 'pulse'}
      {#if scannerMode === 'local'}
        <section class="screen-head local-feed-head">
          <div><span class="eyebrow">LOCAL SCANNER FEED</span><h1>Pulse</h1><p>Only results pulled and ranked by the scanner on this device.</p></div>
          <div class="local-head-actions"><div class="live-state"><i></i><span>{localFeedMessage}</span><small>{providers.filter((provider) => provider.enabled && provider.configured).length} local sources connected</small></div><button class="primary" onclick={runLiveScan} disabled={scanning || !node}>{scanning ? 'Scanning…' : 'Scan now'}</button></div>
        </section>
        {#if activeLocalScan()}
          <section class="local-scan-strip"><span class="local-scan-icon">⌕</span><span><b>Local scan receipt</b><small>{localFeedTime()}</small></span><strong>{activeLocalScan()?.rows.length} candidates</strong><span>{activeLocalScan()?.scanned_symbols || '—'} symbols scanned · {activeLocalScan()?.elapsed_seconds.toFixed(1)}s</span></section>
        {/if}
        <section class="runner-list" aria-busy={scanning}>
          {#each localPulseRows() as row}
            <button class="runner-row local-row" onclick={() => openTicker(localRunnerRow(row), 'pulse')}>
              <span class="ticker-badge">{row.ticker.slice(0, 3)}</span>
              <span class="runner-name"><strong>{row.ticker}</strong><small>Local scanner</small></span>
              <span class="runner-thesis"><b>{row.trade_state}</b><small>{row.state_reason}</small></span>
              <span class="runner-risk"><small>{row.rug_level} risk</small><b>{row.score.toFixed(1)} setup</b></span>
              <span class="runner-price"><strong>${row.price.toFixed(2)}</strong><small class:positive={row.change_pct >= 0}>{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></span><span class="row-arrow">›</span>
            </button>
          {:else}
            <div class="empty-state"><span>◉</span><h2>{scanning ? 'Building your local Pulse' : 'No local scan data yet'}</h2><p>{scanning ? 'The local scanner is pulling current market bars from its connected sources.' : 'Connect the local scanner and run a live scan. Cloud rows are never shown in Local mode.'}</p>{#if !scanning}<button class="primary" onclick={runLiveScan} disabled={!node}>Run local scan</button>{/if}</div>
          {/each}
        </section>
      {:else}
        <section class="screen-head cloud-feed-head">
          <div><span class="eyebrow">RATi CLOUD FEED</span><h1>Pulse</h1><p>Hosted discovery from runners.rati.chat.</p></div>
          <div class="live-state cloud-state"><i></i><span>{feedMessage}</span>{#if feedUpdatedAt}<small>Updated {feedUpdatedAt}</small>{/if}</div>
        </section>
        {#if pulse.flash_record}
          <button class="flash-strip cloud-strip" onclick={() => view = 'flash'}>
            <span class="flash-avatar">ϟ</span><span><b>{pulse.flash_record.label}</b><small>{pulse.flash_record.model_label} · permanent scorecard</small></span>
            <strong>{pulse.flash_record.headline_rate_visible && pulse.flash_record.hit_rate != null ? `${Math.round(pulse.flash_record.hit_rate * 100)}% hit` : `${pulse.flash_record.settled} settled`}</strong>
            <span>{pulse.flash_record.hits} hits · {pulse.flash_record.misses} misses <b>View record ›</b></span>
          </button>
        {/if}
        <section class="runner-list" aria-busy={feedLoading}>
          {#each pulse.rows as row}
            <button class="runner-row cloud-row" onclick={() => openTicker(row, 'pulse')}>
              <span class="ticker-badge">{row.coin_label || row.ticker.slice(0, 3)}</span>
              <span class="runner-name"><strong>{row.ticker}</strong><small>{row.company || row.name || 'Market runner'}</small></span>
              <span class="runner-thesis"><b>{row.pulse_label || row.trade_state || 'Moving'}</b><small>{row.directional_thesis || row.case_thesis || row.case_source_name || 'Fresh verified movement'}</small></span>
              <span class="runner-risk"><small>{row.rug_level ? `${row.rug_level} risk` : row.social_label || row.source || 'public'}</small>{#if row.case_confidence != null}<b>{confidenceText(row.case_confidence)}</b>{/if}</span>
              <span class="runner-price"><strong>{row.price != null ? `$${row.price.toFixed(2)}` : '—'}</strong><small class:positive={Number(row.change_pct || 0) >= 0}>{changeText(row)}</small></span><span class="row-arrow">›</span>
            </button>
          {:else}
            <div class="empty-state"><span>◉</span><h2>No saved cloud Pulse yet</h2><p>Connect once in Cloud mode to load the hosted feed.</p></div>
          {/each}
        </section>
      {/if}
    {:else if view === 'radar'}
      {#if scannerMode === 'local'}
        <section class="screen-head local-feed-head"><div><span class="eyebrow">LOCAL ACTIVITY RADAR</span><h1>Radar</h1><p>{localRadarRows(activeLocalScan()).length} local candidates ranked by relative volume and price movement.</p></div><div class="local-head-actions"><div class="live-state"><i></i><span>{localFeedMessage}</span></div><button class="primary" onclick={runLiveScan} disabled={scanning || !node}>{scanning ? 'Scanning…' : 'Refresh'}</button></div></section>
        <section class="runner-list radar-list" aria-busy={scanning}>
          {#each localRadarRows(activeLocalScan()) as row}
            <button class="runner-row local-row" onclick={() => openTicker(localRunnerRow(row), 'radar')}>
              <span class="radar-sweep"></span><span class="runner-name"><strong>{row.ticker}</strong><small>Local scanner</small></span>
              <span class="runner-thesis"><b>{row.trade_state}</b><small>{row.state_reason}</small></span>
              <span class="event-count"><b>{row.relative_volume == null ? '—' : `${row.relative_volume.toFixed(1)}×`}</b><small>rel volume</small></span>
              <span class="runner-price"><strong>${row.price.toFixed(2)}</strong><small class:positive={row.change_pct >= 0}>{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></span><span class="row-arrow">›</span>
            </button>
          {:else}<div class="empty-state"><span>⌁</span><h2>{scanning ? 'Sweeping local sources' : 'Local Radar is empty'}</h2><p>No cloud events are mixed into Local mode. Run the local scanner to populate Radar.</p>{#if !scanning}<button class="primary" onclick={runLiveScan} disabled={!node}>Run local scan</button>{/if}</div>{/each}
        </section>
      {:else}
        <section class="screen-head cloud-feed-head"><div><span class="eyebrow">RATi CLOUD EVENT WATCH</span><h1>Radar</h1><p>{radar.rows.length} recent hosted events from runners.rati.chat.</p></div><div class="live-state cloud-state"><i></i><span>{feedMessage}</span></div></section>
        <section class="runner-list radar-list" aria-busy={feedLoading}>
          {#each radar.rows as row}
            <button class:updated={row.has_update} class="runner-row cloud-row" onclick={() => openTicker(row, 'radar')}>
              <span class="radar-sweep"></span><span class="runner-name"><strong>{row.ticker}</strong><small>{row.company || row.name || 'Market runner'}</small></span>
              <span class="runner-thesis"><b>{row.pulse_label || 'New event'}</b><small>{row.directional_thesis || row.case_thesis || row.case_source_name || 'Fresh public evidence'}</small></span>
              <span class="event-count"><b>{row.event_count || 1}</b><small>events</small></span>
              <span class="runner-price"><strong>{row.price != null ? `$${row.price.toFixed(2)}` : '—'}</strong><small class:positive={Number(row.change_pct || 0) >= 0}>{changeText(row)}</small></span><span class="row-arrow">›</span>
            </button>
          {:else}<div class="empty-state"><span>⌁</span><h2>Cloud Radar is quiet</h2><p>Fresh hosted events will appear here.</p></div>{/each}
        </section>
      {/if}
    {:else if view === 'flash'}
      {#if scannerMode === 'local'}
        <section class="screen-head flash-heading local-feed-head"><div><span class="eyebrow">LOCAL SCANNER · FLASH ϟ</span><h1>Scan receipts</h1><p>Immutable results created by this scanner. No hosted calls are shown.</p></div><button class="primary" onclick={runLiveScan} disabled={scanning || !node}>{scanning ? 'Scanning…' : 'New local scan'}</button></section>
        {#if activeLocalScan()}
          <section class="record-hero local-record">
            <div><span class="flash-avatar">ϟ</span><p><strong>Local scan receipt</strong><small>{localFeedTime()}</small></p></div>
            <b>{activeLocalScan()?.rows.length}<small>candidates</small></b>
            <p>{activeLocalScan()?.scanned_symbols || '—'} symbols scanned · {activeLocalScan()?.liquid_symbols || '—'} liquid</p><p>{activeLocalScan()?.elapsed_seconds.toFixed(1)} seconds · {activeLocalScan()?.warnings.length || 0} warnings</p>
          </section>
          <section class="record-grid local-record-grid">
            <article class="method-card"><span class="eyebrow">LOCAL RUN</span><h2>Provider status</h2>{#each activeLocalScan()?.warnings || [] as warning}<p class="pull-warning">{warning}</p>{:else}<p>No provider warnings were recorded for this scan.</p>{/each}<small>Only live provider output is stored in local receipts.</small></article>
            <article class="version-card local-receipts"><span class="eyebrow">LOCAL HISTORY</span><h2>Recent scans</h2>{#each receipts.slice(0, 6) as receipt}<button class:active={receipt.id === activeLocalScan()?.id} onclick={() => scan = receipt}><span><b>{receipt.rows.length} candidates</b><small>{new Date(receipt.finished_at).toLocaleString()}</small></span><strong>{receipt.elapsed_seconds.toFixed(1)}s</strong></button>{/each}</article>
          </section>
          <section class="ledger local-ledger"><div class="section-head"><div><span class="eyebrow">LOCAL CANDIDATES</span><h2>Latest ranked results</h2></div></div>
            {#each activeLocalScan()?.rows.slice(0, 10) || [] as row}<button onclick={() => openTicker(localRunnerRow(row), 'flash')}><strong>{row.ticker}</strong><span>{row.trade_state} · {row.score.toFixed(1)} setup</span><b>{row.rug_level} risk</b><small>${row.price.toFixed(2)} · {row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></button>{/each}
          </section>
        {:else}<div class="empty-state"><span>ϟ</span><h2>{scanning ? 'Creating a local receipt' : 'No local scan receipts yet'}</h2><p>Run the scanner to create the first on-device Flash receipt.</p>{#if !scanning}<button class="primary" onclick={runLiveScan} disabled={!node}>Run local scan</button>{/if}</div>{/if}
      {:else}
        <section class="screen-head flash-heading cloud-feed-head"><div><span class="eyebrow">RATi CLOUD · FLASH ϟ</span><h1>Forecast record</h1><p>Hosted model record. Frozen calls. Later prices. No rewrites.</p></div><button class="cloud-button" onclick={() => openRunner('/flash/record')}>Open cloud record ↗</button></section>
        {#if flash?.current_version}
          <section class="record-hero cloud-record">
            <div><span class="flash-avatar">ϟ</span><p><strong>{flash.current_version.label}</strong><small>{flash.current_version.model_label} · {flash.current_version.state.replace('_', ' ')}</small></p></div>
            <b>{flash.current_version.headline_rate_visible && flash.current_version.hit_rate != null ? `${Math.round(flash.current_version.hit_rate * 100)}%` : flash.current_version.settled}<small>{flash.current_version.headline_rate_visible ? 'hit rate' : 'settled'}</small></b>
            <p>{flash.current_version.hits} hits · {flash.current_version.misses} misses · {flash.current_version.no_calls} no calls · {flash.current_version.pending} pending</p><p>{flash.current_version.distinct_tickers} tickers · {flash.current_version.distinct_trading_days || 0} trading days</p>
          </section>
          <section class="record-grid cloud-record-grid">
            <article class="method-card"><span class="eyebrow">CLOUD CONTRACT</span><h2>One fixed finish line</h2><p>An up or down call is judged at the next regular-session close. It is a hit only after a move larger than {flash.contract.minimum_move_pct.toFixed(1)}% in Flash's direction.</p><small>The headline rate appears after {flash.contract.headline_sample} settled forecasts.</small></article>
            <article class="version-card"><span class="eyebrow">CLOUD VERSIONS</span><h2>Permanent scorecards</h2>{#each flash.versions as version}<div><span><b>{version.label}</b><small>{version.model_label} · {version.state}</small></span><strong>{version.settled} settled</strong></div>{/each}</article>
          </section>
          <section class="ledger cloud-ledger"><div class="section-head"><div><span class="eyebrow">CLOUD RECEIPTS</span><h2>Latest public results</h2></div></div>
            {#each flash.recent_results as result}<button onclick={() => openRunner(result.report_url)}><strong>{result.ticker}</strong><span>{result.direction.replace('_', ' ')} · {Math.round(result.probability_up * 100)}% up</span><b class:hit={result.classification === 'hit'} class:miss={result.classification === 'miss'}>{(result.classification || result.status).replace('_', ' ')}</b><small>{result.return_pct != null ? `${result.return_pct >= 0 ? '+' : ''}${result.return_pct.toFixed(1)}% · ` : ''}{result.version_label}</small></button>{:else}<p class="empty-copy">No public forecast receipts yet.</p>{/each}
          </section>
        {:else}<div class="empty-state"><span>ϟ</span><h2>No saved Cloud Flash record</h2><p>Connect in Cloud mode to load the hosted scorecard.</p></div>{/if}
      {/if}
    {:else if view === 'ticker'}
      <section class="ticker-local-head">
        <button class="back-button" onclick={() => view = tickerBackView}>← Back to {tickerBackView}</button>
        <span class="local-badge">Local scanner</span>
      </section>
      {#if tickerLoading}
        <section class="ticker-loading"><span class="local-orbit">⌁</span><h1>{selectedTicker?.ticker || 'Ticker'}</h1><p>Pulling fresh daily and five-minute bars through your local scanner…</p></section>
      {:else if tickerError}
        <section class="offline-library ticker-error"><span class="eyebrow">LOCAL TICKER</span><h2>{selectedTicker?.ticker || 'Ticker'} could not load</h2><p>{tickerError}</p><button onclick={() => view = 'scanner'}>Open Scanner</button></section>
      {:else if tickerDetail}
        <section class="ticker-hero">
          <div><span class="eyebrow">LOCAL TICKER ANALYSIS</span><h1>{tickerDetail.ticker}</h1><p>{selectedTicker?.company || selectedTicker?.name || 'Live market data from your scanner'}</p></div>
          <div class="ticker-quote"><strong>${tickerDetail.quote.price.toFixed(2)}</strong><b class:positive={Number(tickerDetail.quote.change_pct || 0) >= 0}>{tickerDetail.quote.change_pct == null ? '—' : `${tickerDetail.quote.change_pct >= 0 ? '+' : ''}${tickerDetail.quote.change_pct.toFixed(2)}%`}</b><small>{tickerDetail.quote.session} · {new Date(tickerDetail.quote.quote_time).toLocaleString()}</small></div>
        </section>
        {#if selectedTicker?.pulse_label}
          <section class="cloud-context"><span class="cloud-badge">RATi Cloud context</span><strong>{selectedTicker.pulse_label}</strong><p>{selectedTicker.directional_thesis || selectedTicker.case_thesis || 'This discovery context came from runners.rati.chat. The chart and scanner verdict below were pulled locally.'}</p></section>
        {/if}
        <section class="ticker-chart-card">
          <div class="section-head"><div><span class="eyebrow">LOCAL PRICE BARS</span><h2>{tickerChart === 'intraday' ? 'Five-minute chart' : 'Daily chart'}</h2></div><div class="chart-controls"><button class:active={tickerChart === 'intraday'} onclick={() => tickerChart = 'intraday'}>5 minute</button><button class:active={tickerChart === 'daily'} onclick={() => tickerChart = 'daily'}>Daily</button></div></div>
          {#if selectedChartBars().length > 1}
            <svg class="price-chart" viewBox="0 0 1000 250" role="img" aria-label={`${tickerDetail.ticker} ${tickerChart} price chart`}><line x1="20" y1="225" x2="980" y2="225"></line><line x1="20" y1="35" x2="980" y2="35"></line><polyline points={chartPoints(selectedChartBars())}></polyline></svg>
            <div class="chart-caption"><span>{new Date(selectedChartBars()[0].timestamp).toLocaleString()}</span><b>{chartRange(selectedChartBars())} · {selectedChartBars().length} real bars</b><span>{new Date(selectedChartBars()[selectedChartBars().length - 1].timestamp).toLocaleString()}</span></div>
          {:else}<p class="empty-copy">The provider did not return enough bars for this chart.</p>{/if}
        </section>
        {#if tickerDetail.analysis}
          <section class="ticker-metrics"><div><small>Trade state</small><strong>{tickerDetail.analysis.trade_state}</strong></div><div><small>Setup score</small><strong>{tickerDetail.analysis.score.toFixed(1)}</strong></div><div><small>Rug risk</small><strong>{tickerDetail.analysis.rug_level} · {tickerDetail.analysis.rug_score.toFixed(0)}</strong></div><div><small>Relative volume</small><strong>{tickerDetail.analysis.relative_volume == null ? '—' : `${tickerDetail.analysis.relative_volume.toFixed(1)}×`}</strong></div><div><small>Dollar volume</small><strong>{compactNumber(tickerDetail.analysis.dollar_volume)}</strong></div><div><small>VWAP position</small><strong>{tickerDetail.analysis.vwap_position_pct >= 0 ? '+' : ''}{tickerDetail.analysis.vwap_position_pct.toFixed(1)}%</strong></div></section>
          <section class="ticker-evidence-grid"><article><span class="eyebrow">LOCAL VERDICT</span><h2>{tickerDetail.analysis.state_reason}</h2><p>5m momentum {tickerDetail.analysis.momentum_5m_pct >= 0 ? '+' : ''}{tickerDetail.analysis.momentum_5m_pct.toFixed(1)}% · breakout {tickerDetail.analysis.breakout_pct >= 0 ? '+' : ''}{tickerDetail.analysis.breakout_pct.toFixed(1)}%</p></article><article><span class="eyebrow">SIGNALS</span>{#each tickerDetail.analysis.signals as signal}<p class="evidence-line positive-line">+ {signal}</p>{:else}<p class="empty-copy">No positive scanner signals.</p>{/each}</article><article><span class="eyebrow">RISKS</span>{#each tickerDetail.analysis.risks as risk}<p class="evidence-line risk-line">! {risk}</p>{:else}<p class="empty-copy">No additional scanner risks.</p>{/each}</article></section>
        {/if}
        <section class="data-pulls"><div class="section-head"><div><span class="eyebrow">LOCAL DATA PULLS</span><h2>What this scanner fetched</h2></div><small>{new Date(tickerDetail.fetched_at).toLocaleString()}</small></div>{#each tickerDetail.pulls as pull}<article><span class:failed={pull.status === 'failed'} class="pull-status">{pull.status}</span><div><strong>{pull.label}</strong><small>{pull.provider} · {pull.feed}</small></div><b>{pull.bars} bars</b><small>{pull.delayed == null ? 'Freshness unknown' : pull.delayed ? 'Delayed source' : 'Live source'}{pull.fallback_used ? ' · fallback used' : ''}</small></article>{/each}{#each tickerDetail.warnings as warning}<p class="pull-warning">{warning}</p>{/each}</section>
      {/if}
    {:else if view === 'scanner'}
      <section class:cloud-feed-head={scannerMode === 'cloud'} class="screen-head"><div><span class="eyebrow">{scannerMode.toUpperCase()} ENGINE</span><h1>Scanner</h1><p>Run locally on this device or use RATi Cloud.</p></div><span class:cloud-state={scannerMode === 'cloud'} class:online={node} class="connection-dot">{node ? 'Connected' : 'Disconnected'}</span></section>
      <section class:cloud-panel={scannerMode === 'cloud'} class="scanner-hero"><div><span class="eyebrow">SCANNER LOCATION</span><h2>{scannerMode === 'local' ? 'Private and on-device' : 'Managed by RATi Cloud'}</h2><p>{scannerMode === 'local' ? 'Free internet sources are connected by default. Your optional API keys and scan history stay with this scanner.' : 'Connects to runners.rati.chat. No local setup or maintenance.'}</p></div><div class="large-mode-control"><button class:active={scannerMode === 'local'} onclick={() => chooseScannerMode('local')}><b>Local</b><small>This device</small></button><button class="cloud-choice" class:active={scannerMode === 'cloud'} onclick={() => chooseScannerMode('cloud')}><b>Cloud</b><small>runners.rati.chat</small></button></div></section>
      <section class:cloud-panel={scannerMode === 'cloud'} class="connection-panel"><label for="node-url">Scanner address</label><div class="connection-row"><input id="node-url" bind:value={nodeUrl} spellcheck="false" /><button class:cloud-button={scannerMode === 'cloud'} class="primary" onclick={() => refreshScanner()} disabled={connecting}>{connecting ? 'Connecting…' : 'Connect'}</button></div>{#if !isDesktop || scannerMode === 'local'}<label for="node-token">Access token <small>Only needed for a separate self-hosted scanner</small></label><input id="node-token" type="password" bind:value={nodeToken} autocomplete="off" />{/if}<p class="status" aria-live="polite">{scannerMessage}</p></section>
      {#if node}
        <section class:cloud-panel={node.mode === 'cloud'} class="node-summary"><div><small>Mode</small><strong>{node.mode.replace('_', ' ')}</strong></div><div><small>Scanner</small><strong>{node.scanner_version}</strong></div><div><small>Research</small><strong>{node.capabilities.research.replace('_', ' ')}</strong></div><div><small>Sources</small><strong>{providers.filter((provider) => provider.enabled && provider.configured).length} connected</strong></div></section>
        <section class:cloud-panel={node.mode === 'cloud'} class="scan-action"><div><span class="eyebrow">RUN NOW</span><h2>Scan live market data</h2><p>Yahoo powers the scan without an API key. Other enabled no-key sources are connected automatically; optional paid sources can be added in Settings.</p></div><button class:cloud-button={node.mode === 'cloud'} class="primary" onclick={runLiveScan} disabled={scanning || node.mode === 'cloud'}>{scanning ? 'Scanning live data…' : node.mode === 'cloud' ? 'Managed by cloud' : 'Run live scan'}</button></section>
      {:else}<section class="offline-library"><span class="eyebrow">SCANNER OFFLINE</span><h2>Your saved work is still here</h2><p>Pulse, Radar, Flash, and saved receipts continue to work. Connect a scanner only when you want a new local run.</p></section>{/if}
      {#if scan || receipts.length}<section class="scan-section"><div class="section-head"><div><span class="eyebrow">RECEIPT LIBRARY</span><h2>{receipts.length} saved scans</h2></div></div>{#if scan}<div class="scan-list">{#each scan.rows as row}<button class="scan-row" onclick={() => openTicker(row, 'scanner')}><div><strong>{row.ticker}</strong><small>{row.trade_state} · {row.rug_level} risk</small></div><div><b>{row.score.toFixed(1)}</b><small>setup</small></div><div><b>${row.price.toFixed(2)}</b><small>{row.change_pct >= 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</small></div><p>{row.state_reason}</p></button>{/each}</div>{/if}<div class="receipt-grid">{#each receipts as receipt}<button class="receipt-card" onclick={() => scan = receipt}><strong>{receipt.rows.length} candidates</strong><small>{new Date(receipt.finished_at).toLocaleString()} · {receipt.source}</small></button>{/each}</div></section>{/if}
    {:else}
      <section class:cloud-feed-head={scannerMode === 'cloud'} class="screen-head"><div><span class="eyebrow">SCANNER SETTINGS</span><h1>Connections</h1><p>Add your own keys to the selected scanner.</p></div><span class:cloud-state={scannerMode === 'cloud'} class:online={node} class="connection-dot">{scannerMode} · {node ? 'connected' : 'offline'}</span></section>
      {#if node}
        <section class="workspace-grid">
          <article class="card action-card"><span class="eyebrow">OPTIONAL AI</span><h2>OpenRouter</h2><p>{openrouter.status === 'connected' ? `Connected · ${openrouter.connection_method}` : 'Connect with OAuth PKCE or paste your own key.'}</p><div class="button-row">{#if node.mode === 'cloud'}<span>Managed by RATi Cloud</span>{:else if openrouter.status === 'connected'}{#if openrouter.activity_url}<button onclick={() => openExternal(openrouter.activity_url!)}>View usage</button>{/if}<button onclick={disconnectOpenRouter}>Disconnect</button>{:else}<button class="primary" onclick={connectOpenRouter}>Connect OpenRouter</button>{/if}</div>{#if node.mode !== 'cloud' && openrouter.status !== 'connected'}<div class="key-entry"><input type="password" bind:value={openrouterKey} autocomplete="off" placeholder="Or paste an sk-or- API key" aria-label="OpenRouter API key" /><button onclick={connectOpenRouterKey} disabled={openrouterKey.trim().length < 24}>Save key</button></div>{/if}</article>
          <article class="card"><span class="eyebrow">WHAT EACH MODE ADDS</span><h2>Local or Cloud</h2><p><b>Local</b> runs the open-source scanner on this device with your keys. <b>Cloud</b> runs the same app and scanner as a managed service at runners.rati.chat.</p></article>
        </section>
        {#if openrouter.status === 'connected' && node.mode !== 'cloud'}<section class="research-section"><div class="section-head"><div><span class="eyebrow">OPTIONAL AI</span><h2>Research</h2></div></div><textarea bind:value={researchPrompt} maxlength="6000" placeholder="What should RATi research?"></textarea><button class="primary" onclick={runResearch} disabled={researching || researchPrompt.trim().length < 3}>{researching ? 'Researching…' : 'Run research'}</button>{#if research}<article class="research-answer"><small>{research.model} · {new Date(research.generated_at).toLocaleString()}</small><p>{research.answer}</p></article>{/if}</section>{/if}
        <section class:cloud-panel={node.mode === 'cloud'} class="providers-section"><div class="section-head"><div><span class="eyebrow">SOURCE REGISTRY</span><h2>Data sources</h2></div><small>{providers.length} connections</small></div><div class="provider-grid">{#each providers as provider}<article class="provider-card"><div><strong>{provider.title}</strong><span class:ready={provider.state === 'ready' || provider.state === 'connected'}>{provider.state.replace('_', ' ')}</span></div><p>{provider.feeds.map((feed) => feed.title).join(' · ')}</p>{#if provider.feeds[0]?.terms_url}<button class="text-button" onclick={() => openExternal(provider.feeds[0].terms_url!)}>Source terms ↗</button>{/if}{#if node.mode !== 'cloud' && provider.configuration_kind === 'api_key'}{#if provider.configured}<button onclick={() => disconnectProvider(provider)}>Disconnect</button>{:else}<div class="key-entry"><input type="password" value={providerKeys[provider.id] || ''} oninput={(event) => providerKeys = { ...providerKeys, [provider.id]: event.currentTarget.value }} autocomplete="off" placeholder={`${provider.title} API key`} aria-label={`${provider.title} API key`} /><button onclick={() => connectProvider(provider)} disabled={(providerKeys[provider.id] || '').trim().length < 8}>Save key</button></div>{/if}{/if}</article>{/each}</div></section>
      {:else}<section class="offline-library"><span class="eyebrow">SCANNER OFFLINE</span><h2>Connect a scanner first</h2><p>Choose Local or Cloud above, then open Scanner to connect.</p></section>{/if}
    {/if}
  </main>

  <footer>RATi Swarm {runtime.appVersion} · {runtime.platform} · Scanner API v{node?.api_version || '—'} · Copyright © 2026 RATi contributors · AGPL-3.0-only · <button class="footer-link" onclick={() => openExternal('https://github.com/atimics/runner-watch')}>Source</button></footer>
</div>
