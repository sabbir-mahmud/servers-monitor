'use strict';
// The only things the page can ask for: which agents exist, open one's log, follow it,
// show it in the file manager, save the visible lines, and pair this computer. No Node,
// no file paths in.
const { contextBridge, ipcRenderer } = require('electron');

function on(channel, fn) {
  const listener = (_e, payload) => fn(payload);
  ipcRenderer.on(channel, listener);
  return () => ipcRenderer.removeListener(channel, listener);
}

contextBridge.exposeInMainWorld('nexguard', {
  platform: process.platform,
  listSources: () => ipcRenderer.invoke('sources:list'),
  open: (id) => ipcRenderer.invoke('log:open', id),
  reveal: () => ipcRenderer.invoke('log:reveal'),
  exportText: (text) => ipcRenderer.invoke('log:export', text),
  onEntries: (fn) => on('log:entries', fn),
  onStatus: (fn) => on('agent:status', fn),
  onFocusSearch: (fn) => on('ui:focus-search', fn),
  // Pairing: the page sends what the person typed; main checks it again.
  pairInfo: () => ipcRenderer.invoke('pair:info'),
  pendingLink: () => ipcRenderer.invoke('pair:pending'),
  preview: (input) => ipcRenderer.invoke('pair:preview', input),
  connect: (input) => ipcRenderer.invoke('pair:connect', input),
  onPairLink: (fn) => on('pair:link', fn),
  onShowPair: (fn) => on('ui:pair', fn),
});
