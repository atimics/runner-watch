import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const workspaceScript = readFileSync(resolve(process.cwd(), '../web/static/desktop-workspace.js'), 'utf8');

afterEach(() => {
  document.body.innerHTML = '';
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function workspace(desktop = true) {
  vi.stubGlobal('requestAnimationFrame', () => 0);
  vi.stubGlobal('matchMedia', () => ({ matches: desktop, addEventListener: vi.fn() }));
  document.body.innerHTML = `<div data-desktop-workspace>
    <div data-desktop-list>
      <a href="/memecoins/radar" id="radar">Radar</a>
      <a href="/memecoins/coin/dogecoin" id="doge">Dogecoin</a>
      <a href="/memecoins/coin/shiba-inu" id="shib">Shiba Inu</a>
    </div>
    <iframe data-desktop-frame></iframe>
    <div data-desktop-empty></div><div data-desktop-loading hidden></div>
  </div>`;
  window.eval(workspaceScript);
}

describe('web desktop coin panels', () => {
  it('selects a coin by ID and keeps market navigation as full pages', () => {
    workspace();
    const frame = document.querySelector('iframe')!;
    expect(frame.getAttribute('src')).toBe('/memecoins/coin/dogecoin');
    expect(document.querySelector('#doge')?.classList.contains('desktop-panel-selected')).toBe(true);
    const coinClick = new MouseEvent('click', { bubbles: true, cancelable: true });
    document.querySelector('#shib')!.dispatchEvent(coinClick);
    expect(coinClick.defaultPrevented).toBe(true);
    expect(frame.getAttribute('src')).toBe('/memecoins/coin/shiba-inu');
    const navigation = new MouseEvent('click', { bubbles: true, cancelable: true });
    document.querySelector('#radar')!.dispatchEvent(navigation);
    expect(navigation.defaultPrevented).toBe(false);
    expect(frame.getAttribute('src')).toBe('/memecoins/coin/shiba-inu');
  });

  it('opens coin details as full pages at phone width', () => {
    workspace(false);
    const click = new MouseEvent('click', { bubbles: true, cancelable: true });
    document.querySelector('#doge')!.dispatchEvent(click);
    expect(click.defaultPrevented).toBe(false);
    expect(document.querySelector('iframe')!.hasAttribute('src')).toBe(false);
  });
});
