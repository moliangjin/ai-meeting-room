const {contextBridge, ipcRenderer} = require('electron');

contextBridge.exposeInMainWorld('aimrDesktop', Object.freeze({
  getBrainStatus: () => ipcRenderer.invoke('brain:status'),
  openBrainWindow: connectAttemptId => ipcRenderer.invoke('brain:open', {connectAttemptId}),
  getRuntimeStatus: () => ipcRenderer.invoke('runtime:status'),
  runDesktopBrainPoc: () => ipcRenderer.invoke('brain:desktop-poc'),
  onDesktopBrainPocStatus: listener => {
    const wrapped = (_event, status) => listener(status);
    ipcRenderer.on('brain:desktop-poc-status', wrapped);
    return () => ipcRenderer.removeListener('brain:desktop-poc-status', wrapped);
  },
}));
