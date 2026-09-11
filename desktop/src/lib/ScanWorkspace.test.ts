import { afterEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import ScanWorkspace from './ScanWorkspace.svelte';
import { defaultScanRequest, type ScanResult } from './node';

let component: ReturnType<typeof mount> | null = null;
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  document.body.innerHTML = '';
});

function render(overrides: Record<string, unknown> = {}) {
  const props = { available: true, busy: false, message: 'Ready', receipt: null, onscan: vi.fn().mockResolvedValue(undefined), onopen: vi.fn(), ...overrides };
  component = mount(ScanWorkspace, { target: document.body, props });
  flushSync();
  return props;
}

function field(label: string): HTMLInputElement | HTMLSelectElement {
  const element = [...document.querySelectorAll('label')].find(item => item.firstChild?.textContent === label)?.querySelector('input, select');
  if (!element) throw new Error(`Missing field: ${label}`);
  return element as HTMLInputElement | HTMLSelectElement;
}

function fill(label: string, value: string) {
  const element = field(label);
  element.value = value;
  element.dispatchEvent(new Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  flushSync();
}

function submit() {
  document.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
  flushSync();
}

it('sends custom tickers, daily filters, limits, and ranking in one request', () => {
  const props = render();
  fill('Universe', 'custom');
  fill('Tickers', 'aapl, BRK.B;AAPL nvda');
  fill('Price range', '5:20');
  fill('Min daily shares', '250000');
  fill('Min daily value ($)', '1000000');
  fill('Intraday scan limit', '75');
  fill('Result limit', '10');
  fill('Rank by', 'gainers');
  submit();
  expect(props.onscan).toHaveBeenCalledWith({ ...defaultScanRequest(), universe: 'custom', symbols: ['AAPL', 'BRK-B', 'NVDA'], min_price: 5, max_price: 20, min_avg_volume: 250000, min_avg_dollar_volume: 1000000, max_symbols: 75, top_n: 10, sort: 'gainers' });
});

it('explains invalid ranges and custom tickers, then lets the user reset', () => {
  const props = render();
  fill('Min price ($)', '5');
  submit();
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('maximum price');
  expect(props.onscan).not.toHaveBeenCalled();
  fill('Max price ($)', '10');
  fill('Universe', 'custom');
  fill('Tickers', ' , ');
  submit();
  expect(document.querySelector('[role="alert"]')?.textContent).toContain('1 to 500');
  expect(props.onscan).not.toHaveBeenCalled();
  [...document.querySelectorAll('button')].find(item => item.textContent === 'Reset')!.click();
  flushSync();
  submit();
  expect(props.onscan).toHaveBeenCalledWith(defaultScanRequest());
});

const receipt: ScanResult = { id: 'scan-1', status: 'complete', source: 'live', finished_at: '2026-09-08T16:00:00Z', elapsed_seconds: 2, requested_symbols: 5, liquid_symbols: 3, scanned_symbols: 2, matched_symbols: 2, scan_cap_reached: true, result_cap_reached: true, warnings: ['Quotes may be delayed.'], request: { ...defaultScanRequest(), top_n: 1, max_symbols: 2, sort: 'gainers' }, rows: [{ ticker: 'TEST', score: 50, price: 2, change_pct: 10, relative_volume: null, quote_time: '2026-09-08T15:55:00Z', state_reason: 'Watching price', trade_state: 'WATCH', rug_level: 'HIGH', rug_score: 80 }] };

it('restores receipt settings and keeps result evidence separate from draft edits', () => {
  const props = render({ receipt });
  expect(field('Rank by').value).toBe('gainers');
  fill('Rank by', 'losers');
  expect(document.querySelector('.scanner-results')?.textContent).toContain('Biggest gainers');
  expect(document.body.textContent).toContain('Showing the first 1 of 2');
  expect(document.body.textContent).toContain('2 stock limit');
  expect(document.querySelectorAll('dd')[2].textContent).toBe('2');
  expect(document.body.textContent).toContain('Quotes may be delayed.');
  document.querySelector<HTMLButtonElement>('.scanner-result')!.click();
  expect(props.onopen).toHaveBeenCalledWith(expect.objectContaining({ ticker: 'TEST', source_id: 'built-in-scanner' }));
});

it.each([{ busy: true }, { available: false }])('keeps submissions safe when the scanner is busy or offline: %j', (state) => {
  const props = render(state);
  submit();
  expect(props.onscan).not.toHaveBeenCalled();
});

it('shows zero counts and an empty scan with useful next steps', () => {
  render({ receipt: { ...receipt, request: undefined, rows: [], requested_symbols: 0, liquid_symbols: 0, scanned_symbols: 0, matched_symbols: 0, scan_cap_reached: false, result_cap_reached: false } });
  expect([...document.querySelectorAll('dd')].map(item => item.textContent)).toEqual(['0', '0', '0', '0', '0']);
  expect(document.body.textContent).toContain('Adjust the filters');
});
