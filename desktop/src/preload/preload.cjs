const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ratiDesktop', {
  getRuntime: () => ipcRenderer.invoke('desktop:get-runtime'),
  fetchPublic: (path) => ipcRenderer.invoke('desktop:fetch-public', path),
  openExternal: (url) => ipcRenderer.invoke('desktop:open-external', url),
});
