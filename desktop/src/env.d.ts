/// <reference types="vite/client" />

export interface RatiDesktopRuntime {
  appVersion: string;
  nodeUrl: string;
  nodeToken: string;
  platform: string;
  scannerError: string;
  scannerState: 'starting' | 'ready' | 'failed' | 'stopped';
}

export interface RatiDesktopBridge {
  getRuntime(): Promise<RatiDesktopRuntime>;
  fetchPublic<T>(path: string): Promise<T>;
  openExternal(url: string): Promise<boolean>;
}

declare global {
  interface Window {
    ratiDesktop?: RatiDesktopBridge;
    __TAURI_INTERNALS__?: unknown;
  }
}
