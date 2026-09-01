import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { RatiDesktopRuntime } from '../env';

const { invokeMock, isTauriMock } = vi.hoisted(() => ({
  invokeMock: vi.fn(),
  isTauriMock: vi.fn(),
}));

vi.mock('@tauri-apps/api/core', () => ({
  invoke: invokeMock,
  isTauri: isTauriMock,
}));

import { installDesktopBridge } from './desktop';

const readyRuntime: RatiDesktopRuntime = {
  appVersion: '0.1.0',
  nodeUrl: 'http://127.0.0.1:8787',
  nodeToken: 'private-token',
  platform: 'darwin',
  scannerError: '',
  scannerState: 'ready',
};

beforeEach(() => {
  delete window.ratiDesktop;
  invokeMock.mockReset();
  isTauriMock.mockReset();
});

describe('desktop bridge', () => {
  it('uses the supported Tauri runtime check to install the bridge', async () => {
    isTauriMock.mockReturnValue(true);
    invokeMock.mockResolvedValue(readyRuntime);

    installDesktopBridge();

    await expect(window.ratiDesktop?.getRuntime()).resolves.toEqual(readyRuntime);
    expect(invokeMock).toHaveBeenCalledWith('desktop_runtime');
  });

  it('does not install native commands in a browser', () => {
    isTauriMock.mockReturnValue(false);

    installDesktopBridge();

    expect(window.ratiDesktop).toBeUndefined();
    expect(invokeMock).not.toHaveBeenCalled();
  });
});
