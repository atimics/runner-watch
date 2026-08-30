const { app, BrowserWindow, ipcMain, net, protocol, session, shell } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const { randomBytes } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { safePublicDataUrl } = require('./public-data.cjs');

protocol.registerSchemesAsPrivileged([
  {
    scheme: 'rati-app',
    privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true },
  },
]);

// Keep local scanner receipts and connection settings across the RATi Swarm rename.
app.setPath('userData', path.join(app.getPath('appData'), 'rati-desktop'));

const allowedExternalHosts = new Set([
  'openrouter.ai',
  'rati.chat',
  'runners.rati.chat',
  'sports.rati.chat',
  'sec.gov',
  'www.sec.gov',
  'legal.yahoo.com',
  'apewisdom.io',
  'www.gdeltproject.org',
  'bsky.social',
  'massive.com',
  'www.nasdaqtrader.com',
  'www.nasdaq.com',
  'fintel.io',
  'the-odds-api.com',
  'disneytermsofuse.com',
  'github.com',
]);

let scannerProcess = null;
let scannerUrl = process.env.RATI_NODE_URL || 'http://127.0.0.1:8787';
let scannerToken = process.env.RATI_NODE_TOKEN || '';
let scannerError = '';

function stopBundledScanner() {
  if (!scannerProcess || scannerProcess.killed) return;
  if (process.platform === 'win32' && scannerProcess.pid) {
    spawnSync('taskkill', ['/pid', String(scannerProcess.pid), '/T', '/F'], {
      stdio: 'ignore',
      windowsHide: true,
    });
  } else {
    scannerProcess.kill();
  }
}

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value));
    return url.protocol === 'https:' && allowedExternalHosts.has(url.hostname) ? url.href : null;
  } catch {
    return null;
  }
}

async function startBundledScanner() {
  if (!app.isPackaged || process.env.RATI_NODE_URL) return;
  const executable = process.platform === 'win32' ? 'rati-scanner.exe' : 'rati-scanner';
  const scannerPath = path.join(process.resourcesPath, 'scanner', executable);
  if (!fs.existsSync(scannerPath)) {
    scannerUrl = '';
    scannerError = 'The bundled scanner was not found.';
    return;
  }
  scannerToken = randomBytes(32).toString('base64url');
  scannerProcess = spawn(scannerPath, [], {
    env: {
      ...process.env,
      DATABASE_PATH: path.join(app.getPath('userData'), 'rati-scanner.db'),
      RATI_NODE_TOKEN: scannerToken,
      RATI_NODE_HOST: '127.0.0.1',
      RATI_NODE_MODE: 'local',
      RATI_NODE_PORT: '0',
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  await new Promise((resolve) => {
    let settled = false;
    let stdout = '';
    let stderr = '';
    const finish = (error = '') => {
      if (error) {
        scannerError = error;
        scannerUrl = '';
      }
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        resolve();
      }
    };
    const timeout = setTimeout(() => {
      stopBundledScanner();
      finish('The local scanner did not start within 60 seconds.');
    }, 60_000);
    scannerProcess.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
      const lines = stdout.split(/\r?\n/);
      stdout = lines.pop() || '';
      for (const line of lines) {
        try {
          const event = JSON.parse(line);
          if (event.event === 'ready' && typeof event.url === 'string') {
            scannerUrl = event.url;
            scannerError = '';
            finish();
          }
        } catch {
          // Ignore non-protocol output from native dependencies.
        }
      }
    });
    scannerProcess.stderr.on('data', (chunk) => {
      stderr = `${stderr}${chunk.toString()}`.slice(-2_000);
    });
    scannerProcess.on('error', (error) => {
      finish(`The local scanner could not start: ${error.message}`);
      scannerProcess = null;
    });
    scannerProcess.once('exit', (code) => {
      if (!settled) {
        const detail = stderr.trim().split(/\r?\n/).pop();
        finish(detail || `The local scanner exited during startup (${code ?? 'unknown'}).`);
      } else if (code && !app.isQuitting) {
        scannerError = `The local scanner stopped unexpectedly (${code}).`;
      }
      scannerProcess = null;
    });
  });
}

function registerRendererProtocol() {
  const rendererRoot = path.resolve(__dirname, '..', '..', 'dist', 'renderer');
  protocol.handle('rati-app', (request) => {
    const url = new URL(request.url);
    const requestedPath = decodeURIComponent(url.pathname === '/' ? '/index.html' : url.pathname);
    let filePath = path.resolve(rendererRoot, `.${requestedPath}`);
    if (!filePath.startsWith(`${rendererRoot}${path.sep}`) || !fs.existsSync(filePath)) {
      filePath = path.join(rendererRoot, 'index.html');
    }
    return net.fetch(pathToFileURL(filePath).toString());
  });
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 900,
    minHeight: 640,
    backgroundColor: '#07110d',
    show: false,
    title: 'RATi Swarm',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, '..', 'preload', 'preload.cjs'),
    },
  });
  window.once('ready-to-show', () => window.show());
  window.webContents.setWindowOpenHandler(({ url }) => {
    const safe = safeExternalUrl(url);
    if (safe) void shell.openExternal(safe);
    return { action: 'deny' };
  });
  window.webContents.on('will-navigate', (event, url) => {
    let allowed = false;
    try {
      const destination = new URL(url);
      allowed = (destination.protocol === 'rati-app:' && destination.hostname === 'app')
        || destination.origin === 'http://127.0.0.1:5173';
    } catch {
      allowed = false;
    }
    if (!allowed) event.preventDefault();
  });
  const devUrl = process.env.RATI_DESKTOP_DEV_URL;
  void window.loadURL(devUrl || 'rati-app://app/');
}

ipcMain.handle('desktop:get-runtime', () => ({
  appVersion: app.getVersion(),
  nodeUrl: scannerUrl,
  nodeToken: scannerToken,
  platform: process.platform,
  scannerError,
}));

ipcMain.handle('desktop:fetch-public', async (_event, value) => {
  const safe = safePublicDataUrl(value);
  if (!safe) throw new Error('This public data address is not allowed');
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12_000);
  try {
    const response = await net.fetch(safe, {
      credentials: 'omit',
      headers: { Accept: 'application/json' },
      redirect: 'error',
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`RATi Cloud returned ${response.status}`);
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.toLowerCase().includes('application/json')) {
      throw new Error('RATi Cloud returned an invalid response');
    }
    return await response.json();
  } catch (error) {
    if (controller.signal.aborted) throw new Error('RATi Cloud request timed out');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
});

ipcMain.handle('desktop:open-external', async (_event, value) => {
  const safe = safeExternalUrl(value);
  if (!safe) throw new Error('This external address is not allowed');
  await shell.openExternal(safe);
  return true;
});

app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });
  if (!process.env.RATI_DESKTOP_DEV_URL) registerRendererProtocol();
  await startBundledScanner();
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('before-quit', () => {
  app.isQuitting = true;
  stopBundledScanner();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
