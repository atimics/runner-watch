const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('ratiDesktop', {
  getRuntime: () => ipcRenderer.invoke('desktop:get-runtime'),
  openExternal: (url) => ipcRenderer.invoke('desktop:open-external', url),
});
