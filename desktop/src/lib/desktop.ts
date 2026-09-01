import { invoke } from '@tauri-apps/api/core';

import type { RatiDesktopBridge, RatiDesktopRuntime } from '../env';

const STARTUP_POLL_MS = 150;
const STARTUP_TIMEOUT_MS = 60_000;

function isTauri(): boolean {
  return '__TAURI_INTERNALS__' in window;
}

async function getRuntime(): Promise<RatiDesktopRuntime> {
  const deadline = Date.now() + STARTUP_TIMEOUT_MS;
  let runtime = await invoke<RatiDesktopRuntime>('desktop_runtime');
  while (runtime.scannerState === 'starting' && Date.now() < deadline) {
    await new Promise((resolve) => window.setTimeout(resolve, STARTUP_POLL_MS));
    runtime = await invoke<RatiDesktopRuntime>('desktop_runtime');
  }
  if (runtime.scannerState === 'starting') {
    return {
      ...runtime,
      scannerState: 'failed',
      scannerError: 'The local scanner did not start within 60 seconds.',
    };
  }
  return runtime;
}

export function installDesktopBridge(): void {
  if (!isTauri()) return;
  const bridge: RatiDesktopBridge = {
    getRuntime,
    openExternal: (url: string) => invoke<boolean>('open_external', { url }),
  };
  window.ratiDesktop = bridge;
}
