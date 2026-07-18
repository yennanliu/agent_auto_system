'use strict';

const { app, BrowserWindow, dialog, shell, Menu } = require('electron');
const { spawn } = require('node:child_process');
const path = require('node:path');
const net = require('node:net');
const fs = require('node:fs');
const { waitForHealthy } = require('./health');
const { resolveBackend } = require('./lib/backend-config');

// ── Config ───────────────────────────────────────────────────────────────────
// Project root (the repo) sits one level above electron/. In a packaged build
// the frozen backend lives under process.resourcesPath instead (see resolveBackend).
const PROJECT_ROOT = path.join(__dirname, '..');
const IS_PACKAGED = app.isPackaged;

let backendProc = null;
let backendPort = null;
let mainWindow = null;
let shuttingDown = false;

// ── Free-port selection ────────────────────────────────────────────────────────
// Ask the OS for an ephemeral port on loopback, then hand it to uvicorn. Loopback
// binding avoids firewall prompts and keeps the backend unreachable off-machine.
function pickFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

// ── Where the app keeps writable data (DB, uploads, reports, logs) ──────────────
// Never inside the read-only .app bundle — always the per-user app-data dir.
function appDataDir() {
  const dir = path.join(app.getPath('userData'), 'data');
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

// How to launch the backend lives in ./lib/backend-config.js (pure + unit-tested):
//   dev/spike → `uv run uvicorn` from the repo; packaged → frozen binary (Phase 2).
function startBackend(port) {
  const dataDir = appDataDir();
  const { command, args, env, cwd } = resolveBackend({
    port,
    dataDir,
    isPackaged: IS_PACKAGED,
    resourcesPath: process.resourcesPath,
    projectRoot: PROJECT_ROOT,
    baseEnv: process.env,
  });

  const logDir = path.join(dataDir, 'logs');
  fs.mkdirSync(logDir, { recursive: true });
  const logStream = fs.createWriteStream(path.join(logDir, 'backend.log'), { flags: 'a' });
  logStream.write(`\n=== launch ${new Date().toISOString()} :: ${command} ${args.join(' ')} ===\n`);

  const proc = spawn(command, args, { cwd, env });
  proc.stdout.on('data', (d) => logStream.write(d));
  proc.stderr.on('data', (d) => logStream.write(d));
  proc.on('exit', (code, signal) => {
    logStream.write(`=== backend exited code=${code} signal=${signal} ===\n`);
    if (!shuttingDown) {
      dialog.showErrorBox(
        'Agent Auto System',
        `The backend stopped unexpectedly (code ${code}).\nSee logs:\n${logDir}`
      );
      app.quit();
    }
  });
  return proc;
}

function stopBackend() {
  if (!backendProc) return;
  shuttingDown = true;
  try {
    backendProc.kill('SIGTERM');
  } catch (_e) {
    /* already gone */
  }
  // Hard-kill fallback if it doesn't exit promptly.
  const proc = backendProc;
  setTimeout(() => {
    try {
      proc.kill('SIGKILL');
    } catch (_e) {
      /* already gone */
    }
  }, 4000);
  backendProc = null;
}

// ── Windows ──────────────────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: 'Agent Auto System',
    backgroundColor: '#0f1117',
    show: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // Show a splash until the backend is healthy.
  mainWindow.loadFile(path.join(__dirname, 'splash.html'));

  // Open external links in the system browser, not inside the app window.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

async function boot() {
  createWindow();
  backendPort = await pickFreePort();
  backendProc = startBackend(backendPort);

  const healthy = await waitForHealthy(backendPort, { timeoutMs: 90000 });
  if (!mainWindow) return; // user closed during boot
  if (!healthy) {
    dialog.showErrorBox(
      'Agent Auto System',
      'The backend did not become ready in time. Check the logs in your app-data folder.'
    );
    app.quit();
    return;
  }
  await mainWindow.loadURL(`http://127.0.0.1:${backendPort}/`);
}

// ── App lifecycle ────────────────────────────────────────────────────────────
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    Menu.setApplicationMenu(Menu.buildFromTemplate(menuTemplate()));
    boot();
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) boot();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', stopBackend);
  app.on('will-quit', stopBackend);
}

function menuTemplate() {
  const isMac = process.platform === 'darwin';
  return [
    ...(isMac ? [{ role: 'appMenu' }] : []),
    { role: 'editMenu' },
    { role: 'viewMenu' },
    {
      label: 'Help',
      submenu: [
        {
          label: 'Reveal Logs',
          click: () => shell.openPath(path.join(appDataDir(), 'logs')),
        },
      ],
    },
  ];
}
