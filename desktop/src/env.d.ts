/// <reference types="vite/client" />

interface RatiDesktopRuntime {
  appVersion: string;
  nodeUrl: string;
  platform: string;
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
