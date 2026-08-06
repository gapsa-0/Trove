"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("archiveDesktop", Object.freeze({
  chooseFolder: () => ipcRenderer.invoke("archive:choose-folder"),
  about: () => ipcRenderer.invoke("archive:about"),
  copyDiagnostics: () => ipcRenderer.invoke("archive:copy-diagnostics"),
  openDataFolder: () => ipcRenderer.invoke("archive:open-data-folder"),
  revealFile: path => ipcRenderer.invoke("archive:reveal-file", path)
}));
