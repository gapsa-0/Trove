"use strict";

const { app, BrowserWindow, clipboard, dialog, ipcMain, shell } = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { validReady } = require("./ready.cjs");
const { probeSandbox, noSandboxReason } = require("./sandbox.cjs");

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

/* Whether the renderer is sandboxed, recorded rather than decided.

   The decision is not this program's to make: Chromium reads --no-sandbox off
   the command line before it runs any of this file, and answers "neither
   sandbox is available" by aborting -- so a build that reaches this line on a
   system with no sandbox was started with the switch by something outside it.
   That something is scripts/appimage-launcher.sh, which is why only the AppImage
   has one (the .deb installs an AppArmor profile instead and sandboxes
   normally). src/sandbox.cjs holds the reasoning for both.

   What is left to do here is tell the truth about it: an unsandboxed renderer
   should never be something a user can only discover by reading a launcher
   script, so it goes in the diagnostics the About panel copies, and on stderr
   for whoever started Trove from a terminal. */
const sandboxGap = noSandboxReason(probeSandbox(path.join(path.dirname(process.execPath), "chrome-sandbox")));
if (sandboxGap) {
  appendMainDiagnostic(`renderer sandbox off: ${sandboxGap}`);
  console.warn(`Trove: the renderer is not sandboxed -- ${sandboxGap}.`);
}

function writeRotatingLog(name, lines) {
  const directory = app.getPath("logs");
  fs.mkdirSync(directory, { recursive: true });
  const current = path.join(directory, name);
  try {
    if (fs.existsSync(current) && fs.statSync(current).size > 256 * 1024) {
      fs.renameSync(current, `${current}.1`);
    }
  } catch {
    // Best effort. A rotate that fails leaves the log growing, which is never a
    // good enough reason to stop recording diagnostics.
  }
  fs.appendFileSync(current, `${lines.join("\n")}\n`);
  return current;
}

function backendCommand() {
  if (app.isPackaged) {
    const name = process.platform === "win32" ? "trove-backend.exe" : "trove-backend";
    return { command: path.join(process.resourcesPath, "backend", name), args: [] };
  }
  if (process.env.ARCHIVE_BACKEND) return { command: process.env.ARCHIVE_BACKEND, args: [] };
  // Running the source checkout is intentionally explicit and does not use trove gui,
  // because that command opens a browser itself.
  // Resolve explicit relative paths from the terminal's working directory. The
  // backend itself starts from the checkout root, so passing one unchanged would
  // otherwise resolve it from a different directory.
  const configuredPython = process.env.PYTHON;
  const python = configuredPython && (configuredPython.startsWith(".") || path.isAbsolute(configuredPython))
    ? path.resolve(process.cwd(), configuredPython)
    : configuredPython || (process.platform === "win32" ? "python" : "python3");
  return { command: python, args: ["-m", "trove.desktop"] };
}

async function verifyHealth(port) {
  const response = await fetch(`http://${LOOPBACK}:${port}/api/health`, { signal: AbortSignal.timeout(5_000) });
  const health = await response.json();
  if (!response.ok || health.ok !== true) throw new Error("backend health check failed");
}

// PyInstaller's one-dir layout puts everything but the launcher under _internal/,
// so the staged tools land at backend/_internal/tools -- not backend/tools, which
// is what this used to point at. Nothing failed visibly, because the backend also
// finds them through sys._MEIPASS; the cost was a diagnostics panel that reported
// every bundled tool as "missing". It matters more now that the tools directory is
// also where ffmpeg's shared libraries live (trove.runtime.tool_env).
function bundledToolRoot() {
  if (!app.isPackaged) return process.env.ARCHIVE_TOOLS_DIR || null;
  return path.join(process.resourcesPath, "backend", "_internal", "tools");
}

function startBackend() {
  const spec = backendCommand();
  backend = spawn(spec.command, [...spec.args, "--host", LOOPBACK, "--port", "0"], {
    cwd: app.isPackaged ? process.resourcesPath : path.resolve(__dirname, "../.."),
    shell: false,
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, ARCHIVE_TOOLS_DIR: bundledToolRoot() || "" }
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
  if (backendPort === null) return false;
  const origin = `http://${LOOPBACK}:${backendPort}`;
  return url === origin || url.startsWith(`${origin}/`);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1360, height: 880, minWidth: 980, minHeight: 680,
    // `plugins` gates Chromium's built-in PDF viewer, which is what renders a
    // document in the media viewer's stage. Without it the <iframe> the viewer
    // points at /file/<id> stays blank in the packaged app while working fine
    // in a browser -- the one difference that would make the feature look
    // broken only after shipping.
    //
    // It does not re-enable NPAPI or any external plugin: those were removed
    // from Chromium years ago, and this flag now controls only the bundled
    // internal viewers. The window keeps nodeIntegration off, contextIsolation
    // on and the renderer sandboxed, and will-navigate below still confines it
    // to the loopback origin.
    webPreferences: { preload: path.join(__dirname, "preload.cjs"), nodeIntegration: false, contextIsolation: true, sandbox: true, plugins: true }
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

// ffmpeg is a shared build: a small binary beside the libav* libraries it links
// against. So "the file is there" stopped being the same question as "it runs" --
// a staging bug that omitted the libraries leaves an executable that exists and
// dies on every invocation with a loader error. Actually run it. `-version` is
// cheap, and the diagnostic is only built when a user asks for it.
function toolStatus(name, toolRoot) {
  if (!toolRoot) return "development PATH";
  const exe = path.join(toolRoot, `${name}${process.platform === "win32" ? ".exe" : ""}`);
  if (!fs.existsSync(exe)) return "missing";
  const probe = spawnSync(exe, [name === "exiftool" ? "-ver" : "-version"], {
    encoding: "utf8",
    timeout: 10000,
    // Mirrors trove.runtime.tool_env: the loader needs the staged
    // directory, and upstream's RPATH is broken so nothing else supplies it.
    env: process.platform === "win32" ? process.env
      : { ...process.env, LD_LIBRARY_PATH: [toolRoot, process.env.LD_LIBRARY_PATH].filter(Boolean).join(path.delimiter) },
  });
  if (probe.status === 0) return `bundled (${(probe.stdout || "").split("\n")[0].trim() || "ok"})`;
  return `bundled but will not run: ${(probe.stderr || probe.error?.message || "unknown error").split("\n")[0].trim()}`;
}

function diagnosticText() {
  const tools = ["exiftool", "ffprobe", "ffmpeg"].map(name => `${name}: ${toolStatus(name, bundledToolRoot())}`);
  const sandbox = `Renderer sandbox: ${sandboxGap ? `off (${sandboxGap})` : "on"}`;
  return [`Trove ${app.getVersion()} (${process.env.ARCHIVE_BUILD_COMMIT || "dev"})`, `OS: ${process.platform} ${process.arch}`, `Data folder: ${app.getPath("userData")}`, sandbox, ...tools, "Recent local errors:", ...stderrLines.slice(-20), ...mainLines.slice(-20)].join("\n");
}

ipcMain.handle("archive:about", async () => ({ version: app.getVersion(), commit: process.env.ARCHIVE_BUILD_COMMIT || "dev", backendVersion: backendPort === null ? "not running" : app.getVersion(), dataFolder: app.getPath("userData") }));
ipcMain.handle("archive:copy-diagnostics", async () => { clipboard.writeText(diagnosticText()); return true; });
ipcMain.handle("archive:open-data-folder", async () => shell.openPath(app.getPath("userData")));

/* Show a file in the OS file manager, for the viewer's "Open file location".

   The renderer is confined to the loopback origin, but it is still the place a
   path arrives from, so this does not hand an arbitrary string to the shell. The
   backend is asked which folders it has registered as archive roots, and the
   path has to resolve to somewhere inside one of them -- which is exactly the
   set of files the app is allowed to be showing in the first place. `resolve`
   before the comparison so `..` cannot climb out of a root it starts inside. */
ipcMain.handle("archive:reveal-file", async (event, target) => {
  if (!mainWindow || event.senderFrame !== mainWindow.webContents.mainFrame || !onlyArchiveOrigin(event.senderFrame.url)) {
    throw new Error("untrusted reveal request");
  }
  if (typeof target !== "string" || !target) throw new Error("no path given");
  const resolved = path.resolve(target);
  const response = await fetch(`http://${LOOPBACK}:${backendPort}/api/archives`, { signal: AbortSignal.timeout(5_000) });
  const { archives = [] } = await response.json();
  const inside = archives.some(a => {
    if (!a.path) return false;
    const root = path.resolve(a.path);
    return resolved === root || resolved.startsWith(root + path.sep);
  });
  if (!inside) throw new Error("path is not inside a registered archive");
  shell.showItemInFolder(resolved);
  return true;
});

async function showStartupFailure(error) {
  appendMainDiagnostic(`startup failure: ${error.stack || error.message}`);
  const log = writeDiagnostics();
  const choice = await dialog.showMessageBox({ type: "error", buttons: ["Copy diagnostics", "Quit"], defaultId: 1,
    message: "Trove could not start its local catalogue service.", detail: `${error.message}\n\nDiagnostics: ${log}` });
  if (choice.response === 0) clipboard.writeText(diagnosticText());
}

app.whenReady().then(async () => {
  try { await startBackend(); await createWindow(); }
  catch (error) { await showStartupFailure(error); await stopBackend(); app.quit(); }
});
app.on("before-quit", event => { if (backend?.exitCode === null) { event.preventDefault(); stopBackend().finally(() => app.exit()); } });
app.on("window-all-closed", () => app.quit());
process.on("uncaughtException", error => { appendMainDiagnostic(`uncaught exception: ${error.stack || error.message}`); writeDiagnostics(); dialog.showErrorBox("Trove encountered an error", "A local diagnostic record was saved. Please restart Trove."); });
process.on("unhandledRejection", error => { appendMainDiagnostic(`unhandled rejection: ${error?.stack || error}`); writeDiagnostics(); });
