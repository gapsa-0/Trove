"use strict";

const { app, BrowserWindow, clipboard, dialog, ipcMain, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const { validReady } = require("./ready.cjs");

const LOOPBACK = "127.0.0.1";
const READY_TIMEOUT_MS = 20_000;
let backend = null;
let backendPort = null;
let mainWindow = null;
const stderrLines = [];
const mainLines = [];

function appendDiagnostic(line) {
  stderrLines.push(line);
  if (stderrLines.length > 200) stderrLines.shift();
}

function appendMainDiagnostic(line) {
  mainLines.push(`${new Date().toISOString()} ${line}`);
  if (mainLines.length > 200) mainLines.shift();
}

function writeRotatingLog(name, lines) {
  const directory = app.getPath("logs");
  fs.mkdirSync(directory, { recursive: true });
  const current = path.join(directory, name);
  try { if (fs.existsSync(current) && fs.statSync(current).size > 256 * 1024) fs.renameSync(current, `${current}.1`); } catch (_) {}
  fs.appendFileSync(current, `${lines.join("\n")}\n`);
  return current;
}

function backendCommand() {
  if (app.isPackaged) {
    const name = process.platform === "win32" ? "organize-archive-backend.exe" : "organize-archive-backend";
    return { command: path.join(process.resourcesPath, "backend", name), args: [] };
  }
  if (process.env.ARCHIVE_BACKEND) return { command: process.env.ARCHIVE_BACKEND, args: [] };
  // Running the source checkout is intentionally explicit and does not use oa gui,
  // because that command opens a browser itself.
  const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
  return { command: python, args: ["-m", "organize_archive.desktop"] };
}

async function verifyHealth(port) {
  const response = await fetch(`http://${LOOPBACK}:${port}/api/health`, { signal: AbortSignal.timeout(5_000) });
  const health = await response.json();
  if (!response.ok || health.ok !== true) throw new Error("backend health check failed");
}

function startBackend() {
  const spec = backendCommand();
  backend = spawn(spec.command, [...spec.args, "--host", LOOPBACK, "--port", "0"], {
    cwd: app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "../.."),
    shell: false,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, ARCHIVE_TOOLS_DIR: app.isPackaged ? path.join(process.resourcesPath, "backend", "tools") : process.env.ARCHIVE_TOOLS_DIR || "" }
  });
  backend.stderr.setEncoding("utf8");
  backend.stderr.on("data", text => {
    const lines = text.split(/\r?\n/).filter(Boolean);
    lines.forEach(appendDiagnostic);
    writeRotatingLog("backend-stderr.log", lines);
  });
  backend.stdout.setEncoding("utf8");
  return new Promise((resolve, reject) => {
    let buffer = "";
    const timer = setTimeout(() => reject(new Error("Timed out waiting for backend readiness.")), READY_TIMEOUT_MS);
    const fail = error => { clearTimeout(timer); reject(error); };
    backend.once("error", fail);
    backend.once("exit", code => fail(new Error(`Backend exited before readiness (code ${code}).`)));
    backend.stdout.on("data", async text => {
      buffer += text;
      const lines = buffer.split(/\r?\n/); buffer = lines.pop();
      for (const line of lines) {
        const ready = validReady(line);
        if (!ready) { if (line) appendDiagnostic(`stdout: ${line}`); continue; }
        clearTimeout(timer);
        try { await verifyHealth(ready.port); backendPort = ready.port; resolve(); }
        catch (error) { reject(error); }
        return;
      }
    });
  });
}

async function stopBackend() {
  if (!backend || backend.exitCode !== null) return;
  const child = backend;
  const exited = new Promise(resolve => child.once("exit", resolve));
  child.kill("SIGTERM");
  await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 4_000))]);
  if (child.exitCode === null) child.kill("SIGKILL");
  backend = null;
}

function onlyArchiveOrigin(url) {
  return backendPort !== null && url === `http://${LOOPBACK}:${backendPort}/`;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1360, height: 880, minWidth: 980, minHeight: 680,
    webPreferences: { preload: path.join(__dirname, "preload.cjs"), nodeIntegration: false, contextIsolation: true, sandbox: true }
  });
  mainWindow.webContents.on("will-navigate", (event, url) => { if (!onlyArchiveOrigin(url)) event.preventDefault(); });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (onlyArchiveOrigin(url)) return { action: "allow" };
    if (url.startsWith("https:")) shell.openExternal(url);
    return { action: "deny" };
  });
  return mainWindow.loadURL(`http://${LOOPBACK}:${backendPort}/`);
}

ipcMain.handle("archive:choose-folder", async event => {
  if (!mainWindow || event.senderFrame !== mainWindow.webContents.mainFrame || !onlyArchiveOrigin(event.senderFrame.url)) {
    throw new Error("untrusted folder-picker request");
  }
  const picked = await dialog.showOpenDialog(mainWindow, { properties: ["openDirectory"] });
  return picked.canceled ? { cancelled: true } : { path: picked.filePaths[0] };
});

function writeDiagnostics() {
  const directory = app.getPath("logs");
  fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(directory, "build-info.json"), JSON.stringify({ version: app.getVersion(), commit: process.env.ARCHIVE_BUILD_COMMIT || "dev", platform: process.platform, arch: process.arch }, null, 2));
  if (mainLines.length) writeRotatingLog("electron-main.log", mainLines);
  if (stderrLines.length) writeRotatingLog("backend-stderr.log", stderrLines);
  return directory;
}

function diagnosticText() {
  const toolRoot = app.isPackaged ? path.join(process.resourcesPath, "backend", "tools") : process.env.ARCHIVE_TOOLS_DIR;
  const exe = name => path.join(toolRoot || "", `${name}${process.platform === "win32" ? ".exe" : ""}`);
  const tools = ["exiftool", "ffprobe", "ffmpeg"].map(name => `${name}: ${toolRoot ? (fs.existsSync(exe(name)) ? "bundled" : "missing") : "development PATH"}`);
  return [`Archive ${app.getVersion()} (${process.env.ARCHIVE_BUILD_COMMIT || "dev"})`, `OS: ${process.platform} ${process.arch}`, `Data folder: ${app.getPath("userData")}`, ...tools, "Recent local errors:", ...stderrLines.slice(-20), ...mainLines.slice(-20)].join("\n");
}

ipcMain.handle("archive:about", async () => ({ version: app.getVersion(), commit: process.env.ARCHIVE_BUILD_COMMIT || "dev", backendVersion: backendPort === null ? "not running" : app.getVersion(), dataFolder: app.getPath("userData") }));
ipcMain.handle("archive:copy-diagnostics", async () => { clipboard.writeText(diagnosticText()); return true; });
ipcMain.handle("archive:open-data-folder", async () => shell.openPath(app.getPath("userData")));

async function showStartupFailure(error) {
  appendMainDiagnostic(`startup failure: ${error.stack || error.message}`);
  const log = writeDiagnostics();
  const choice = await dialog.showMessageBox({ type: "error", buttons: ["Copy diagnostics", "Quit"], defaultId: 1,
    message: "Archive could not start its local catalogue service.", detail: `${error.message}\n\nDiagnostics: ${log}` });
  if (choice.response === 0) clipboard.writeText(diagnosticText());
}

app.whenReady().then(async () => {
  try { await startBackend(); await createWindow(); }
  catch (error) { await showStartupFailure(error); await stopBackend(); app.quit(); }
});
app.on("before-quit", event => { if (backend?.exitCode === null) { event.preventDefault(); stopBackend().finally(() => app.exit()); } });
app.on("window-all-closed", () => app.quit());
process.on("uncaughtException", error => { appendMainDiagnostic(`uncaught exception: ${error.stack || error.message}`); writeDiagnostics(); dialog.showErrorBox("Archive encountered an error", "A local diagnostic record was saved. Please restart Archive."); });
process.on("unhandledRejection", error => { appendMainDiagnostic(`unhandled rejection: ${error?.stack || error}`); writeDiagnostics(); });
