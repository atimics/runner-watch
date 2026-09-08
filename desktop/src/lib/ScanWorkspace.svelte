<script lang="ts">
  import { defaultScanRequest, type ScanRequest, type ScanResult } from './node';
  import { sourcedRows, type SourcedScanRow } from './local-feed';

  let { available, busy, message, receipt, onscan, onopen }: {
    available: boolean;
    busy: boolean;
    message: string;
    receipt: ScanResult | null;
    onscan: (request: ScanRequest) => Promise<void>;
    onopen: (row: SourcedScanRow) => void;
  } = $props();

  let filters = $state(defaultScanRequest());
  let symbols = $state('');
  let error = $state('');
  let restored = $state(false);
  let edited = $state(false);
  const ranks = { score: 'Setup score', volume: 'Relative volume', gainers: 'Biggest gainers', losers: 'Biggest losers', price_asc: 'Price: low to high', price_desc: 'Price: high to low' };
  $effect(() => {
    if (receipt?.request && !restored && !edited) {
      filters = { ...defaultScanRequest(), ...receipt.request };
      symbols = filters.symbols.join(', ');
      restored = true;
    }
  });

  function preset(event: Event) {
    const value = (event.currentTarget as HTMLSelectElement).value;
    if (value === 'custom') return;
    const [min, max] = value.split(':').map(Number);
    filters.min_price = min;
    filters.max_price = max;
  }

  function reset() {
    edited = true;
    filters = defaultScanRequest();
    symbols = '';
    error = '';
  }

  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (busy || !available) return;
    error = '';
    if (filters.max_price <= filters.min_price) {
      error = 'Choose a maximum price above the minimum.';
      return;
    }
    const tickers = [...new Set(symbols.split(/[\s,;]+/).filter(Boolean).map(value => value.toUpperCase().replaceAll('.', '-')))];
    if (filters.universe === 'custom' && (!tickers.length || tickers.length > 500 || tickers.some(value => !/^[A-Z][A-Z0-9-]{0,11}$/.test(value)))) {
      error = 'Enter 1 to 500 stock symbols, such as AAPL, NVDA, or BRK-B.';
      return;
    }
    edited = true;
    await onscan({ ...filters, symbols: filters.universe === 'custom' ? tickers : [] });
  }
</script>

<form class="scan-controls" onsubmit={submit} oninput={() => { edited = true; error = ''; }} aria-label="Stock scanner filters">
  <fieldset disabled={busy}>
    <div class="scan-filter-grid">
      <label>Universe<select bind:value={filters.universe}><option value="penny">Penny stocks</option><option value="starter">Starter list</option><option value="broad">US stocks</option><option value="custom">Custom tickers</option></select></label>
      <label>Price range<select onchange={preset} value={filters.min_price === 0.2 && filters.max_price === 5 ? '0.2:5' : filters.min_price === 5 && filters.max_price === 20 ? '5:20' : filters.min_price === 20 && filters.max_price === 100 ? '20:100' : 'custom'}><option value="0.2:5">$0.20–$5</option><option value="5:20">$5–$20</option><option value="20:100">$20–$100</option><option value="custom">Custom range</option></select></label>
      <label>Min price ($)<input type="number" min="0" step="any" required bind:value={filters.min_price} /></label>
      <label>Max price ($)<input type="number" min="0.000001" step="any" required bind:value={filters.max_price} /></label>
      <label>Min daily shares<input type="number" min="0" step="1" required bind:value={filters.min_avg_volume} /></label>
      <label>Min daily value ($)<input type="number" min="0" step="any" required bind:value={filters.min_avg_dollar_volume} /></label>
    </div>
    {#if filters.universe === 'custom'}<label class="scan-symbols">Tickers<input bind:value={symbols} placeholder="AAPL, NVDA, BRK-B" maxlength="6500" required aria-describedby="scan-symbol-help" /><small id="scan-symbol-help">Up to 500 symbols, separated by spaces or commas.</small></label>{/if}
    <p class="scan-help">Price uses the previous close. Daily shares use the median of up to 20 completed sessions. Daily value is shares × previous close.</p>
    <details class="scan-more"><summary>More options</summary><div class="scan-filter-grid">
      <label>Intraday scan limit<input type="number" min="1" max="5000" step="1" required bind:value={filters.max_symbols} /></label>
      <label>Result limit<input type="number" min="1" max="100" step="1" required bind:value={filters.top_n} /></label>
      <label class="scan-checkbox"><input type="checkbox" bind:checked={filters.crash_only} /> Down 60% from the 90-day or yearly high</label>
    </div><p class="scan-help">Daily filters apply first. The scan limit controls how many passing stocks receive an intraday check.</p></details>
    <div class="scan-actions"><label>Rank by<select bind:value={filters.sort}>{#each Object.entries(ranks) as [value, label]}<option {value}>{label}</option>{/each}</select></label><button class="primary" type="submit" disabled={!available}>{busy ? 'Scanning…' : 'Run scanner'}</button><button type="button" onclick={reset}>Reset</button></div>
  </fieldset>
  {#if error}<p class="scan-error" role="alert">{error}</p>{/if}
  <p class="status" role="status">{message}</p>
</form>

{#if receipt}
  <section class="scanner-results" aria-label="Latest scan results">
    <div class="section-head"><h2>Results</h2><small>Completed {new Date(receipt.finished_at).toLocaleString()} · {receipt.elapsed_seconds.toFixed(1)}s</small></div>
    {#if receipt.request}<p class="scan-help">{receipt.request.universe === 'custom' ? `${receipt.request.symbols.length} custom tickers` : { penny: 'Penny stocks', starter: 'Starter list', broad: 'US stocks' }[receipt.request.universe]} · Previous close ${receipt.request.min_price}–${receipt.request.max_price} · {ranks[receipt.request.sort]}{receipt.request.crash_only ? ' · Down 60% from high' : ''}</p>{/if}
    <dl class="scan-counts"><div><dt>Requested</dt><dd>{receipt.requested_symbols ?? '—'}</dd></div><div><dt>Passed daily filters</dt><dd>{receipt.liquid_symbols ?? '—'}</dd></div><div><dt>Intraday data</dt><dd>{receipt.scanned_symbols ?? '—'}</dd></div><div><dt>Usable results</dt><dd>{receipt.matched_symbols ?? '—'}</dd></div><div><dt>Shown</dt><dd>{receipt.rows.length}</dd></div></dl>
    {#if receipt.scan_cap_reached}<p class="scan-help">Intraday coverage reached the {receipt.request?.max_symbols} stock limit. Raise the limit for wider coverage.</p>{/if}
    {#if receipt.result_cap_reached}<p class="scan-help">Showing the first {receipt.rows.length} of {receipt.matched_symbols} usable results, ranked by {receipt.request ? ranks[receipt.request.sort].toLowerCase() : 'setup score'}.</p>{/if}
    <div class="scanner-table"><div class="scanner-columns" aria-hidden="true"><span>Ticker / quote time</span><span>Price</span><span>Change</span><span>Rel volume</span><span>Setup</span></div>
      {#each receipt.rows as row}
        <button class="scanner-result" onclick={() => onopen(sourcedRows([{ ...receipt, rows: [row] }])[0])}><span><strong>{row.ticker}</strong><small>{row.quote_time ? new Date(row.quote_time).toLocaleString() : 'Quote time unavailable'}</small></span><span>${row.price.toFixed(2)}</span><span class:positive={row.change_pct >= 0}>{row.change_pct > 0 ? '+' : ''}{row.change_pct.toFixed(1)}%</span><span>{row.relative_volume == null ? '—' : `${row.relative_volume.toFixed(1)}×`}</span><span>{row.score.toFixed(1)}</span></button>
      {:else}<p class="scan-help">The scan returned 0 usable results. Adjust the filters or refresh when more quote data is available.</p>{/each}
    </div>
    {#each receipt.warnings as warning}<p class="pull-warning">{warning}</p>{/each}
  </section>
{/if}

<style>
  .scan-controls { border-block: 1px solid #26372c; padding: 18px 0; }
  fieldset { padding: 0; margin: 0; border: 0; min-width: 0; }
  .scan-filter-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 14px; }
  label { display: grid; align-content: start; gap: 6px; color: #9aaa9f; font-size: 11px; min-width: 0; }
  input, select { min-width: 0; width: 100%; padding: 9px; border: 1px solid #35443b; border-radius: 0; background: #050806; color: #e7f1eb; font: inherit; font-size: 13px; }
  input:focus-visible, select:focus-visible { outline: 2px solid #60e594; outline-offset: 2px; }
  .scan-symbols { margin-top: 14px; }
  .scan-help { color: #8c9c93; font-size: 11px; line-height: 1.6; margin: 12px 0; }
  .scan-more { border-top: 1px solid #202a24; padding: 12px 0; }
  summary { cursor: pointer; color: #bbc8bf; font-size: 12px; }
  .scan-more .scan-filter-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-top: 14px; }
  .scan-checkbox { display: flex; align-items: center; align-self: end; min-height: 38px; line-height: 1.5; }
  .scan-checkbox input { width: auto; }
  .scan-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }
  .scan-actions label { width: 200px; margin-right: auto; }
  .scan-actions button { min-height: 38px; border-radius: 0; }
  .scan-error { color: #f1a38f; font-size: 12px; }
  .scanner-results { margin-top: 26px; }
  .section-head { flex-wrap: wrap; gap: 8px; }
  h2 { margin: 0; }
  .scan-counts { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 12px; padding: 14px 0; border-block: 1px solid #26372c; }
  dt { color: #8c9c93; font-size: 10px; }
  dd { margin: 6px 0 0; font-size: 19px; font-variant-numeric: tabular-nums; }
  .scanner-columns, .scanner-result { display: grid; grid-template-columns: minmax(120px, 2fr) repeat(4, minmax(55px, 1fr)); gap: 10px; align-items: center; text-align: right; }
  .scanner-columns { padding: 10px 0; font-size: 10px; color: #8c9c93; }
  .scanner-result { width: 100%; padding: 14px 0; border: 0; border-top: 1px solid #202a24; border-radius: 0; background: transparent; color: #e7f1eb; font-size: 13px; font-variant-numeric: tabular-nums; }
  .scanner-result:hover { background: #0c1710; }
  .scanner-columns > :first-child, .scanner-result > :first-child { text-align: left; }
  .scanner-result small { margin-top: 5px; font-size: 9px; }
  .positive { color: #60e594; }
  @media (max-width: 1100px) {
    .scan-filter-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 600px) {
    .scan-more .scan-filter-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .scan-checkbox { grid-column: 1 / -1; }
    .scan-counts { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .scan-actions label { width: 100%; }
    .scanner-columns, .scanner-result { grid-template-columns: minmax(80px, 1.6fr) repeat(4, minmax(0, 1fr)); gap: 6px; font-size: 11px; }
    .scanner-result small { font-size: 8px; }
  }
</style>
