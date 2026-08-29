/// <reference types="vite/client" />

interface RatiDesktopRuntime {
  appVersion: string;
  nodeUrl: string;
  nodeToken: string;
  platform: string;
  scannerError: string;
}

interface RatiDesktopBridge {
  getRuntime(): Promise<RatiDesktopRuntime>;
  openExternal(url: string): Promise<boolean>;
}

declare global {
  interface Window {
    ratiDesktop?: RatiDesktopBridge;
  }
}

export {};
