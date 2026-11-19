'use strict';
// NexGuard for Windows, Mac and Linux. It pairs this computer with a NexGuard dashboard
// through the agent bundled with it, then shows that agent's status and log, live.
//
// Reading the log is read-only and offline: status.json and logs/nexguard.log are readable
// without administrator rights. The network is used only to pair: one preview request to
// the dashboard the person typed (or opened a nexguard://pair link for), so they can read
// what will be shared before anything is installed.
const { app, BrowserWindow, ipcMain, shell, dialog, Menu, nativeTheme } = require('electron');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { candidates } = require('./paths');
const { Tailer, readAll } = require('./tail');
const agent = require('./agent');
const { checkServer, normalizeCode, parseDeepLink } = require('./pairing');

let win = null;
let tailer = null;
let statusTimer = null;
let current = null; // the source being shown
let pendingLink = null; // a nexguard://pair link that arrived before the page asked
let connecting = false;

function bundledAgent() {
  return agent.agentPath({ isPackaged: app.isPackaged, resourcesPath: process.resourcesPath, appDir: path.join(__dirname, '..') });
}

function pairInput({ server, code }) {
  const s = checkServer(server);
  if (!s.ok) throw new Error(s.error);
  const c = normalizeCode(code);
  if (!c) throw new Error('Enter the 8-character pairing code from the dashboard, like K7PQ-3MXA.');
  return { server: s.server, code: c };
}

// A pair link only fills in the form: the person still reads the disclosure and says yes.
function handleLink(url) {
  const link = parseDeepLink(url);
  if (!link) return;
  pendingLink = link;
  if (win && !win.isDestroyed()) {
    send('pair:link', link);
    if (win.isMinimized()) win.restore();
    win.focus();
  }
}

function linkFromArgv(argv) {
  const url = argv.find((a) => typeof a === 'string' && a.startsWith('nexguard://'));
  if (url) handleLink(url);
}

function sources() {
  return candidates({ platform: process.platform, env: process.env, home: os.homedir() });
}

async function exists(p) {
  try {
    await fs.access(p);
    return true;
  } catch {
    return false;
  }
}

async function readStatus(src) {
  try {
    const s = JSON.parse(await fs.readFile(src.status, 'utf8'));
    return {
      at: typeof s.at === 'string' ? s.at : null,
      ok: s.ok === true,
      error: typeof s.error === 'string' ? s.error : '',
      interval: Number.isFinite(s.interval) ? s.interval : 0,
    };
  } catch {
    return null;
  }
}

async function describe(src) {
  const [installed, hasLog, status] = await Promise.all([exists(src.dir), exists(src.log), readStatus(src)]);
  return { id: src.id, label: src.label, dir: src.dir, log: src.log, installed, hasLog, status };
}

async function listSources() {
  return Promise.all(sources().map(describe));
}

function send(channel, payload) {
  if (win && !win.isDestroyed()) win.webContents.send(channel, payload);
}

function stopFollowing() {
  if (tailer) tailer.stop();
  tailer = null;
  clearInterval(statusTimer);
  statusTimer = null;
}

async function open(id) {
  const src = sources().find((s) => s.id === id);
  if (!src) throw new Error('unknown source');
  stopFollowing();
  current = src;
  const entries = await readAll(src.log).catch((err) => {
    throw new Error(err.code === 'EACCES' || err.code === 'EPERM'
      ? `This account can't read ${src.log}.`
      : `Couldn't read ${src.log}: ${err.message}`);
  });
  tailer = new Tailer(src.log, (more, info) => send('log:entries', { id, entries: more, rotated: info.rotated }));
  await tailer.start();
  statusTimer = setInterval(async () => send('agent:status', await describe(src)), 5000);
  return { source: await describe(src), entries };
}

function isOurs(event) {
  return win && event.sender === win.webContents;
}

function handle(channel, fn) {
  ipcMain.handle(channel, (event, ...args) => {
    if (!isOurs(event)) throw new Error('not allowed');
    return fn(...args);
  });
}

handle('sources:list', listSources);
handle('pair:pending', () => {
  const link = pendingLink;
  pendingLink = null;
  return link;
});
handle('pair:info', async () => ({ hasAgent: await exists(bundledAgent()), platform: process.platform }));
handle('pair:preview', async (input) => {
  const { server, code } = pairInput(input || {});
  return agent.preview({ agent: bundledAgent(), server, code, protectedInstall: Boolean(input.protectedInstall) });
});
handle('pair:connect', async (input) => {
  if (connecting) throw new Error('Already connecting.');
  const { server, code } = pairInput(input || {});
  connecting = true;
  try {
    // NEXGUARD_CONFIG_DIR wins, as it does for the agent; otherwise this person's own folder.
    const userDir = (sources().find((s) => s.id === 'custom') || sources().find((s) => s.id === 'user'))?.dir;
    return await agent.connect({ agent: bundledAgent(), server, code, protectedInstall: Boolean(input.protectedInstall), userDir });
  } finally {
    connecting = false;
  }
});
handle('log:open', (id) => open(String(id)));
handle('log:reveal', async () => {
  if (!current) return;
  if (await exists(current.log)) shell.showItemInFolder(current.log);
  else if (await exists(current.dir)) await shell.openPath(current.dir);
});
handle('log:export', async (text) => {
  const { canceled, filePath } = await dialog.showSaveDialog(win, {
    title: 'Save log',
    defaultPath: path.join(app.getPath('documents'), `nexguard-log-${new Date().toISOString().slice(0, 10)}.txt`),
    filters: [{ name: 'Text', extensions: ['txt', 'log'] }],
  });
  if (canceled || !filePath) return false;
  await fs.writeFile(filePath, String(text), 'utf8');
  return true;
});

function createWindow() {
  win = new BrowserWindow({
    width: 1080,
    height: 720,
    minWidth: 560,
    minHeight: 420,
    title: 'NexGuard',
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#111113' : '#f3f3f0',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });
  win.once('ready-to-show', () => win.show());
  win.on('closed', () => {
    stopFollowing();
    win = null;
  });
  // A log viewer never navigates or opens windows.
  win.webContents.on('will-navigate', (e) => e.preventDefault());
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

function menu() {
  const mac = process.platform === 'darwin';
  return Menu.buildFromTemplate([
    ...(mac ? [{ role: 'appMenu' }] : []),
    {
      label: 'File',
      submenu: [
        { label: 'Connect this computer…', accelerator: 'CmdOrCtrl+N', click: () => send('ui:pair') },
        { type: 'separator' },
        mac ? { role: 'close' } : { role: 'quit' },
      ],
    },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { label: 'Find', accelerator: 'CmdOrCtrl+F', click: () => send('ui:focus-search') },
        { type: 'separator' },
        { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
        ...(app.isPackaged ? [] : [{ role: 'toggleDevTools' }]),
      ],
    },
    { role: 'windowMenu' },
  ]);
}

// nexguard:// links open this app (registered by the installer; this covers dev runs).
if (process.defaultApp && process.argv.length >= 2) {
  app.setAsDefaultProtocolClient('nexguard', process.execPath, [path.resolve(process.argv[1])]);
} else {
  app.setAsDefaultProtocolClient('nexguard');
}

app.on('open-url', (event, url) => { // macOS
  event.preventDefault();
  handleLink(url);
});

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', (_event, argv) => { // Windows and Linux pass the link here
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
    linkFromArgv(argv);
  });
  linkFromArgv(process.argv);
  app.whenReady().then(() => {
    Menu.setApplicationMenu(menu());
    createWindow();
    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });
  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });
}
