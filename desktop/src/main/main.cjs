const { app, BrowserWindow, ipcMain, net, protocol, session, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

protocol.registerSchemesAsPrivileged([
  {
    scheme: 'rati-app',
    privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true },
  },
]);

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
  'fintel.io',
  'the-odds-api.com',
  'disneytermsofuse.com',
]);

let scannerProcess = null;
let scannerUrl = process.env.RATI_NODE_URL || 'http://127.0.0.1:8787';

function safeExternalUrl(value) {
  try {
    const url = new URL(String(value));
    return url.protocol === 'https:' && allowedExternalHosts.has(url.hostname) ? url.href : null;
  } catch {
    return null;
  }
}

function freeLoopbackPort() {
  return new Promise((resolve, reject) => {
    const server = http.createServer();
    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : 8787;
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
  });
}

async function startBundledScanner() {
  if (!app.isPackaged || process.env.RATI_NODE_URL) return;
  const executable = process.platform === 'win32' ? 'rati-scanner.exe' : 'rati-scanner';
  const scannerPath = path.join(process.resourcesPath, 'scanner', executable);
  if (!fs.existsSync(scannerPath)) return;
  const port = await freeLoopbackPort();
  scannerUrl = `http://127.0.0.1:${port}`;
  scannerProcess = spawn(scannerPath, [], {
    env: {
      ...process.env,
      DATABASE_PATH: path.join(app.getPath('userData'), 'rati-scanner.db'),
      RATI_NODE_HOST: '127.0.0.1',
      RATI_NODE_MODE: 'local',
      RATI_NODE_PORT: String(port),
    },
    stdio: 'ignore',
    windowsHide: true,
  });
  scannerProcess.once('exit', () => {
    scannerProcess = null;
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
    title: 'RATi',
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
    const allowed = url.startsWith('rati-app://app') || url.startsWith('http://127.0.0.1:5173');
    if (!allowed) event.preventDefault();
  });
  const devUrl = process.env.RATI_DESKTOP_DEV_URL;
  void window.loadURL(devUrl || 'rati-app://app/');
}

ipcMain.handle('desktop:get-runtime', () => ({
  appVersion: app.getVersion(),
  nodeUrl: scannerUrl,
  platform: process.platform,
}));

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
  if (scannerProcess && !scannerProcess.killed) scannerProcess.kill();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
