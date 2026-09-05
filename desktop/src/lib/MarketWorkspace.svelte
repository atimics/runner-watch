<script lang="ts">
  import { onMount } from 'svelte';
  import { callMarkExpired, callStatusLabel, priceHistorySegments, quoteExpired } from './market-view';
  import { NodeClient, type CoinCall, type CoinDetail, type CoinMarket, type CoinSort, type MarketTab, type SportsMarket, type SportsRow } from './node';

  export let market: 'memecoins' | 'sports';
  export let tab: MarketTab;
  export let enabled = false;
  export let available = false;
  export let nodeUrl: string;
  export let nodeToken: string;
  export let onconnect: () => Promise<void>;
  export let openExternal: (url: string) => void;

  let coins: CoinMarket | null = null;
  let calls: CoinCall[] = [];
  let sports: SportsMarket | null = null;
  let detail: CoinDetail | null = null;
  let game: SportsRow | null = null;
  let gameUpdatedAt: string | undefined;
  let now = Date.now();
  let query = '';
  let sort: CoinSort = 'volume';
  let loading = false;
  let connecting = false;
  let error = '';
  let requestVersion = 0;

  $: loadContext(market, tab, enabled);

  onMount(() => {
    const timer = window.setInterval(() => {
      now = Date.now();
      if (enabled && !loading && document.visibilityState !== 'hidden') void load();
    }, 60_000);
    return () => {
      window.clearInterval(timer);
      requestVersion += 1;
    };
  });

  function client() { return new NodeClient(nodeUrl, nodeToken); }
  function title() { return market === 'memecoins' ? 'Memecoins' : 'Sports'; }
  function tabTitle() { return tab === 'pulse' ? 'Pulse' : tab === 'radar' ? 'Radar' : 'Alpha'; }
  function percent(value: number | null | undefined) {
    return value == null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(1)}%`;
  }
  function time(value: string | null | undefined) {
    if (!value || !Number.isFinite(Date.parse(value))) return 'Awaiting source time';
    return new Date(value).toLocaleString();
  }
  function gameTitle(row: SportsRow) {
    return row.company || `${row.away_abbreviation || 'Away'} at ${row.home_abbreviation || 'Home'}`;
  }
  function gameId(row: SportsRow) { return row.id || row.event_id || ''; }
  function gameUrl(row: SportsRow) { return `https://sports.rati.chat/game/${encodeURIComponent(gameId(row))}`; }
  function coinUrl(coinId: string) { return `https://runners.rati.chat/memecoins/coin/${encodeURIComponent(coinId)}`; }
  function openGame(row: SportsRow) {
    game = row;
    gameUpdatedAt = sports?.updated_at;
  }

  function loadContext(_market: string, _tab: MarketTab, connected: boolean) {
    query = '';
    sort = _tab === 'radar' ? 'market_cap' : 'volume';
    coins = null;
    sports = null;
    calls = [];
    detail = null;
    game = null;
    requestVersion += 1;
    error = '';
    loading = false;
    if (connected) void load();
  }

  async function load() {
    const version = ++requestVersion;
    loading = true;
    now = Date.now();
    error = '';
    try {
      const api = client();
      if (detail) {
        const result = await api.memecoin(detail.coin.id);
        if (version === requestVersion) detail = result;
        return;
      }
      if (market === 'sports') {
        const result = await api.sports(tab);
        if (version === requestVersion) {
          sports = result;
          if (game) {
            const updated = (tab === 'alpha' ? result.rows : result.events)?.find(row => gameId(row) === gameId(game!));
            if (updated) openGame(updated);
          }
        }
      } else if (tab === 'alpha') {
        const result = await api.memecoinCalls();
        if (version === requestVersion) calls = result.calls;
      } else {
        const result = await api.memecoins(query, sort, tab === 'pulse' ? 'pulse' : 'radar');
        if (version === requestVersion) coins = result;
      }
    } catch (failure) {
      if (version === requestVersion) error = failure instanceof Error ? failure.message : 'The market feed is unavailable';
    } finally {
      if (version === requestVersion) loading = false;
    }
  }

  async function openCoin(coinId: string) {
    const version = ++requestVersion;
    loading = true;
    error = '';
    detail = null;
    try {
      const result = await client().memecoin(coinId);
      if (version === requestVersion) detail = result;
    } catch (failure) {
      if (version === requestVersion) error = failure instanceof Error ? failure.message : 'Coin detail is unavailable';
    } finally {
      if (version === requestVersion) loading = false;
    }
  }

  async function connect() {
    connecting = true;
    error = '';
    try { await onconnect(); }
    catch (failure) { error = failure instanceof Error ? failure.message : 'Could not enable RATi Cloud'; }
    finally { connecting = false; }
  }
</script>

<section class="screen-head">
  <div><span class="eyebrow">{title()} · RATi Cloud</span><h1>{tabTitle()}</h1>
    <p>{market === 'memecoins' ? tab === 'alpha' ? 'Public paper Calls with entry, mark, and result.' : 'USD prices, daily moves, and source evidence.' : tab === 'alpha' ? 'Public paper Calls on sports outcomes.' : 'Saved projections, market moves, and scores.'}</p>
  </div>
  {#if enabled}<button class="primary" onclick={load} disabled={loading}>Refresh</button>{/if}
</section>

{#if error}<p class="market-notice" role="alert">{error}</p>{/if}
{#if !enabled}
  <section class="empty-state"><h2>Connect RATi Cloud</h2><p>Enable the shared source to browse Memecoins and Sports in this app.</p><button class="primary" onclick={connect} disabled={!available || connecting}>{connecting ? 'Connecting…' : 'Enable RATi Cloud'}</button></section>
{:else if detail}
  <section class="market-detail">
    <button class="text-button" onclick={() => detail = null}>← Back to {tabTitle()}</button>
    <div class="section-head"><div><span class="eyebrow">{detail.coin.symbol} · {detail.coin.id}</span><h2>{detail.coin.name}</h2></div><div class="market-value"><strong>{detail.coin.price_label}</strong><small>{percent(detail.coin.change_24h)} · 24h</small></div></div>
    {#if quoteExpired(detail.coin, detail.collected_at, now) || detail.status !== 'ok' || detail.refresh_failed}<p class="market-notice">Saved quote · {quoteExpired(detail.coin, detail.collected_at, now) ? 'stale' : detail.status}{detail.refresh_failed ? ' · source refresh delayed' : ''}</p>{/if}
    <div class="market-facts"><span>24h volume <b>{detail.coin.volume_label}</b></span><span>Market cap <b>{detail.coin.market_cap_label}</b></span><span>Source <b>{detail.source}</b></span></div>
    {#if priceHistorySegments(detail.history).length}<svg class="market-chart" viewBox="0 0 800 190" role="img" aria-label={`${detail.coin.name} saved price history`}>{#each priceHistorySegments(detail.history) as segment}{#if segment.count > 1}<polyline fill="none" stroke="currentColor" stroke-width="2" points={segment.points} />{:else}<circle cx={segment.x} cy={segment.y} r="2" fill="currentColor" />{/if}{/each}</svg>{:else}<p class="market-caption">Price history appears after two source observations.</p>{/if}
    <p class="market-caption">Source observed {time(detail.evidence.observed_at)} · Collected {time(detail.collected_at)}</p>
    <div class="market-actions"><button onclick={() => openExternal(coinUrl(detail!.coin.id))}>Open coin and paper Calls ↗</button><button onclick={() => openExternal(`https://www.coingecko.com/en/coins/${encodeURIComponent(detail!.coin.id)}`)}>CoinGecko evidence ↗</button></div>
    <h3>Public paper Calls</h3>
    {#each detail.calls || [] as call}<div class="market-call"><span>@{call.caller_handle}<small>{callStatusLabel(call.status)}</small></span><span>Entry {call.entry_price_label}<small>Mark {callMarkExpired(call, now) ? 'expired' : call.mark_price_label}</small></span><b>{callMarkExpired(call, now) ? '—' : percent(call.return_pct)}</b></div>{:else}<p class="market-caption">The first paper Call will appear here.</p>{/each}
  </section>
{:else if game}
  <section class="market-detail">
    <button class="text-button" onclick={() => game = null}>← Back to {tabTitle()}</button>
    <span class="eyebrow">{game.league || game.pulse_label || 'Sports'}</span><h2>{gameTitle(game)}</h2>
    <p>{game.radar_detail || game.status_detail || 'Public market snapshot'}</p>
    {#if game.model_winner_team_name}<p>Projected winner: <b>{game.model_winner_team_name}</b> · {game.model_winner_probability_pct?.toFixed(1) || '—'}%</p>{/if}
    <div class="market-facts">{#if game.market_probability_pct != null}<span>Market probability <b>{game.market_probability_pct.toFixed(1)}%</b></span>{/if}{#if game.away_score != null}<span>Score <b>{game.away_score} – {game.home_score ?? '—'}</b></span>{/if}{#if game.odds_label}<span>Current odds <b>{game.odds_label}</b></span>{/if}{#if game.total_calls != null}<span>Paper Calls <b>{game.active_calls || 0} open · {game.total_calls} total</b></span>{/if}</div>
    <p class="market-caption">{time(game.start_time || gameUpdatedAt)}</p>
    <button class="primary" onclick={() => openExternal(gameUrl(game!))}>Open game evidence and paper Calls ↗</button>
  </section>
{:else}
  {#if market === 'memecoins' && tab !== 'alpha'}
    <form class="market-search" onsubmit={(event) => { event.preventDefault(); void load(); }}><input aria-label="Search memecoins" placeholder="Name, symbol, or coin ID" maxlength="80" bind:value={query}><select aria-label="Sort memecoins" bind:value={sort}><option value="volume">24h volume</option><option value="market_cap">Market cap</option><option value="gainers">Gainers</option><option value="losers">Losers</option></select><button type="submit" disabled={loading}>Search</button></form>
    {#if coins}<p class="market-caption">{coins.source} · {coins.rows.length} of {coins.total} coins · Collected {time(coins.collected_at)}</p>{#if coins.status !== 'ok' || coins.refresh_failed}<p class="market-notice">Feed status: {coins.status}{coins.refresh_failed ? ' · source refresh delayed' : ''}</p>{/if}{/if}
  {/if}
  {#if loading}<p class="market-caption" role="status">Loading {title()}…</p>{/if}
  <section class="runner-list market-list" aria-label={`${title()} ${tabTitle()} results`}>
    {#if market === 'sports'}
      {#if sports?.source_error}<p class="market-notice">{sports.source_error}</p>{/if}
      {#each (tab === 'alpha' ? sports?.rows : sports?.events) || [] as row}<button class="market-row" onclick={() => openGame(row)}><span><strong>{gameTitle(row)}</strong><small>{row.league?.toUpperCase() || row.pulse_label || 'Sports'} · {row.radar_detail || row.model_winner_team_name || `${row.active_calls || 0} open Calls`}</small></span><span><b>{row.price_label || row.radar_label || (row.model_winner_probability_pct != null ? `${row.model_winner_probability_pct.toFixed(1)}%` : '—')}</b><small>{row.status_detail || time(row.start_time)}</small></span><span aria-hidden="true">›</span></button>{:else}{#if !loading && !error}<p class="market-caption">Saved {tabTitle()} results will appear here as the source updates.</p>{/if}{/each}
    {:else if tab === 'alpha'}
      {#each calls as call}<button class="market-row" onclick={() => openCoin(call.coin_id)}><span><strong>{call.name} · {call.symbol}</strong><small>@{call.caller_handle} · {callStatusLabel(call.status)}</small></span><span><b>{callMarkExpired(call, now) ? '—' : percent(call.return_pct)}</b><small>Entry {call.entry_price_label} · Mark {callMarkExpired(call, now) ? 'expired' : call.mark_price_label}</small></span><span aria-hidden="true">›</span></button>{:else}{#if !loading && !error}<p class="market-caption">Public paper Calls will appear here after someone opens a coin Call.</p>{/if}{/each}
    {:else}
      {#each coins?.rows || [] as coin}<button class="market-row" onclick={() => openCoin(coin.id)}><span><strong>{coin.name} · {coin.symbol}</strong><small>{coin.id} · Volume {coin.volume_label}{quoteExpired(coin, coins?.collected_at, now) ? ' · saved quote' : ''}</small></span><span><b>{coin.price_label}</b><small>{percent(coin.change_24h)} · 24h</small></span><span aria-hidden="true">›</span></button>{:else}{#if !loading && !error}<p class="market-caption">{query ? 'Try another name, symbol, or coin ID.' : 'Saved coin quotes will appear after the next source update.'}</p>{/if}{/each}
    {/if}
  </section>
  {#if market === 'sports' && sports?.updated_at}<p class="market-caption">Source updated {time(sports.updated_at)}</p>{/if}
{/if}
