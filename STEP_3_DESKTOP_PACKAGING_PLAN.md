# Step 3 — Ship organize_archive as a desktop application

## Goal

Package the completed archive experience as an installable, self-contained
Windows and Linux desktop application.  Users should launch **Archive** from an
application shortcut—not run Python, install a browser, manage a virtualenv, or
type a local URL.

Use **Electron** as a deliberately thin native shell and the existing Python
application as a bundled localhost sidecar.  Do not rewrite the catalogue engine,
SQLite queries, job manager, or the COA Noir frontend in JavaScript/Rust.

```text
Electron main process
  ├── starts bundled Python backend on 127.0.0.1:0
  ├── waits for explicit readiness
  ├── owns native folder selection and window lifetime
  └── loads the existing local web UI

Python sidecar
  ├── initializes per-user catalogue data
  ├── serves UI/API/media only on loopback
  └── stops when Electron exits
```

## Step 2 launch contract (verified)

- `serve(cfg, host, port)` calls `cfg.ensure_dirs()`, initializes the SQLite schema,
  and supports `port=0` for an OS-selected free port.
- A clean profile can request `/api/archives` and receives `{"archives": []}`.
- `cmd_gui` currently opens a browser itself. Electron must **not** invoke this
  command, because it would create a second browser window.
- There is not yet a health endpoint, machine-readable ready signal, or graceful
  job-manager shutdown hook. Add those as part of this step.

## Product decisions

- Product name: **Archive** (use `organize_archive` as the technical package ID).
- Application ID: choose and reserve a reverse-DNS ID, e.g.
  `io.capsa.organize-archive`. Keep it stable forever once a release ships.
- Scope: x64 Windows and x64 Linux first. Do not promise arm64 until its native
  Python/OpenCV/ONNX build has been tested.
- Windows release: signed, per-user **NSIS `.exe` installer**.
- Linux release: **AppImage** for broad portable use and **`.deb`** for
  Debian/Ubuntu/Mint users. Linux does not have one universally installable format;
  publish both with clear labels.
- No auto-update in the first public build. A manual in-app “Check the release
  page” link is sufficient. Add a trusted update channel only after signing and
  release hosting are established.

Electron-builder identifies NSIS as the normal consumer Windows installer and
supports AppImage and `.deb` Linux targets; AppImage is portable rather than a
system installer. [Target guide](https://www.electron.build/docs/targets/),
[Linux target documentation](https://www.electron.build/docs/linux/)

## Repository structure

Add a small desktop project without contaminating the Python package:

```text
desktop/
├── package.json
├── package-lock.json
├── electron-builder.yml
├── src/
│   ├── main.cjs             # window, sidecar lifecycle, native dialogs
│   └── preload.cjs          # minimal, typed IPC bridge
├── build/
│   ├── icon.ico             # Windows application/installer icon
│   ├── icon.png             # Linux icon (at least 512px)
│   └── installer-sidebar.bmp # optional NSIS artwork, on-brand
└── scripts/
    ├── build-backend.ps1
    ├── build-backend.sh
    └── smoke-test.cjs

organize_archive/
├── desktop.py               # sidecar-specific entry point
└── gui/
    └── ...                  # existing frontend stays here

packaging/
├── organize-archive.spec    # PyInstaller collection rules, if needed
└── requirements-desktop.txt # pinned runtime build profile
```

Generated output belongs under ignored directories such as `desktop/dist/`,
`desktop/release/`, `build/`, and `dist/`; never commit bundled executables or
`node_modules`.

## Implementation plan

### 1. Add a sidecar-only Python entry point

Create `organize_archive.desktop` rather than overloading `oa gui`.

Required CLI contract:

```text
organize-archive-backend --host 127.0.0.1 --port 0
```

- Bind **only** `127.0.0.1`; reject all other host values in the desktop entry
  point. Never listen on LAN interfaces.
- Create the server with port `0`, then read `httpd.server_address[1]`.
- Print one machine-readable readiness line to stdout only after initialization:
  `READY {"port": 49152, "version": "0.1.0"}`.
- Send diagnostics and tracebacks to stderr, never stdout, so Electron can parse
  readiness reliably.
- Add `GET /api/health` returning `{ "ok": true, "version": ... }`; Electron
  must verify it after reading the ready line.
- Handle `SIGINT` and `SIGTERM`: stop accepting work, request `JobManager`
  cancellation/shutdown, call `server_close()`, and exit within a bounded timeout.
- Keep `oa gui` intact as a development/browser-launch command. It may call shared
  server-start helper code, but must retain its current behavior.

Add backend tests that start the desktop entry with `port=0`, parse readiness,
verify `/api/health`, and terminate it cleanly using temporary app-data paths.

### 2. Create the Electron shell

Install Electron and electron-builder in `desktop` as pinned dev dependencies.

`main.cjs` must:

1. Locate the packaged backend at `process.resourcesPath/backend/...` in a release
   and the local development build when running from the repository.
2. Spawn it with `--host 127.0.0.1 --port 0`, `windowsHide: true`, no shell, and
   piped stdout/stderr.
3. Wait at most 20 seconds for the exact ready record, then request `/api/health`.
   If either fails, show an actionable native error dialog with a “Copy diagnostics”
   action and exit—never show a blank window.
4. Create one `BrowserWindow` at a sensible initial size (e.g. 1360×880, minimum
   980×680), load exactly `http://127.0.0.1:{port}/`, and preserve normal OS window
   controls.
5. On `before-quit` and `window-all-closed`, ask the sidecar to exit, wait briefly,
   then terminate it as a fallback. Ensure Windows does not leave a background
   `organize-archive-backend.exe` process.
6. Capture the final ~200 stderr lines in a log under Electron's user-data/log
   directory for error reporting; never send them remotely.

BrowserWindow security is mandatory: `nodeIntegration: false`,
`contextIsolation: true`, `sandbox: true`, no `<webview>`, no disabled web security,
and navigation/new-window handlers that allow only the exact loopback origin.
Electron recommends context isolation and its security guidance specifically warns
against enabling Node integration for loaded content. [Context isolation](https://www.electronjs.org/docs/latest/tutorial/context-isolation),
[Electron security checklist](https://www.electronjs.org/docs/latest/tutorial/security)

### 3. Add the minimal native bridge

The only initial renderer privilege should be folder picking.

- In `preload.cjs`, expose `window.archiveDesktop.chooseFolder()` through
  `contextBridge`.
- In the main process, implement it with `dialog.showOpenDialog({ properties:
  ['openDirectory'] })` and return either `{ cancelled: true }` or `{ path }`.
- Validate IPC sender/frame before fulfilling it.
- Update `index.html` so the welcome screen uses the native picker when the bridge
  exists, then passes the selected absolute path to the existing `/api/archives`
  endpoint.
- Keep the typed path field as a browser/developer fallback. Do not expose generic
  filesystem, shell, child-process, or arbitrary IPC APIs to the page.

### 4. Build the Python runtime profile

Define a deterministic desktop runtime profile in `packaging/requirements-desktop.txt`.

- Include the desired standard features: media metadata/thumbnails, HEIF support,
  deduplication, face detection/clustering, and their native dependencies.
- Exclude remote semantic search by default: it requires a third-party API key and
  conflicts with the product's local-first promise. It can become an explicit,
  separately documented future feature.
- Bundle `ffmpeg`/`ffprobe` and ExifTool as platform-specific application resources
  under the backend bundle; make their paths discoverable via a small runtime helper
  rather than depending on `PATH`.
- Retain clear UI messaging if a deliberately optional capability is absent.
- Pin every Python build dependency and record its license/source. The model files
  downloaded into the cache stay user data and are never embedded in the installer.

Use a **PyInstaller one-directory** backend bundle, not `--onefile`: it starts
faster, works better with OpenCV/ONNX native libraries, makes diagnostics clearer,
and Electron can include the directory as an `extraResource`. PyInstaller supports
collecting package data, binaries, and submodules; keep the collection policy in a
versioned spec file rather than a long opaque release command. [PyInstaller usage
documentation](https://pyinstaller.org/en/stable/usage.html)

The spec must explicitly include:

- `organize_archive/gui/index.html`, `gui/vendor/*`, map icons, and all package
  data;
- `organize_archive/db/schema.sql`;
- Pillow/HEIF/OpenCV/ONNX/scikit-learn/Numpy hidden imports and native binaries;
- bundled `ffmpeg`, `ffprobe`, and ExifTool;
- any required metadata files for packages that inspect installed distributions.

Build the backend **on each target OS**. Do not assume a Linux PyInstaller build is
usable on Windows or vice versa.

### 5. Configure electron-builder and assets

Create `electron-builder.yml` with:

- stable `appId`, product name `Archive`, version sourced from one documented
  release version; do not allow Python and npm versions to drift;
- `files` limited to Electron runtime files;
- `extraResources` that places the PyInstaller output at `backend/`;
- Windows target `nsis`, per-machine install disabled, one-click install disabled
  so the user sees a normal destination/confirm flow, Start-menu and desktop
  shortcut enabled;
- Linux targets `AppImage` and `deb`, category `Graphics` or `Utility`, a proper
  desktop entry, icon, and no terminal;
- a COA Noir / personal-terracotta application icon and installer art, designed
  independently of the UI but visually consistent with it.

Do not rely on Electron's default icon. Validate Windows `.ico` contains multiple
sizes and Linux PNG assets include 512px.

### 6. Development and release commands

Provide one documented command per job:

```text
npm run dev             # build/find dev backend, launch Electron with logs
npm run build:backend   # native PyInstaller bundle for current OS
npm run package:win     # NSIS artifact (from a Windows build environment)
npm run package:linux   # AppImage + .deb (from Linux)
npm run smoke:package   # launch packaged app and validate health/window lifecycle
```

Do not claim a Windows production artifact has been tested when only cross-built
from Linux. Use a Windows runner/VM for Windows packaging and a Linux runner/VM for
Linux packaging. AppImage itself must be built on Linux or in its supported Docker
workflow. [AppImage build requirements](https://www.electron.build/appimage/)

### 7. Code signing and release policy

- Before distributing publicly to Windows users, obtain a Windows code-signing
  certificate and configure signing only through CI secrets/local secure key storage.
  Never commit certificates, passwords, or release credentials.
- Unsigned installers can trigger SmartScreen warnings; document that internal
  testing may use unsigned builds, but do not present them as a polished public
  release.
- Generate checksums for every release artifact and publish a short install guide
  that explains the difference between Windows installer, Linux AppImage, and
  `.deb`.
- Add auto-updates only in a later step with signed release feeds and rollback
  testing.

## Validation matrix

### Automated

- Python test suite including desktop-entry readiness, health, and shutdown tests.
- Electron unit test for ready-line parsing and loopback-origin validation.
- Packaged smoke test on each OS: launch, wait for health, open/close a window,
  assert the child process exits.
- Build artifact checks: backend executable exists; GUI/vendor/schema assets are
  present; external binaries resolve from the bundle.

### Manual Windows

1. Install using a clean non-admin Windows account.
2. Start from the Start menu, select a folder with the native picker, and begin an
   archive.
3. Close and reopen; confirm the catalogue persists under `%LOCALAPPDATA%`.
4. Confirm no original media changes and no Python/browser console appears.
5. Uninstall; verify app binaries are removed while catalogue-data retention is
   clearly stated and behaves as documented.

### Manual Linux

1. Run the AppImage on a supported x64 distribution and install the `.deb` on a
   clean Debian/Ubuntu environment.
2. Confirm menu icon/desktop entry for `.deb`, native folder picker, persistence in
   XDG data, and clean shutdown.
3. If AppImage needs FUSE on a target distro, document the fallback explicitly;
   the `.deb` remains the recommended installable path for Debian-family users.

## Definition of done

- A user can install and start Archive on clean x64 Windows and Linux without
  Python, a venv, or a separately installed browser.
- The Electron window is the only visible UI, and it reliably manages the Python
  sidecar's start, readiness, and shutdown.
- First-run onboarding uses a native folder picker in desktop builds.
- All mutable catalogue data stays in the Step 1 per-user data locations;
  updating/uninstalling the program never touches original media.
- Production BrowserWindow security settings and loopback-only backend access are
  tested.
- NSIS, AppImage, and `.deb` artifacts are reproducible from documented native
  build commands and pass the validation matrix.

## Explicitly out of scope

- macOS packaging and notarization.
- Automatic updates, telemetry, accounts, cloud sync, or remote access.
- New catalogue/search/face capabilities.
- A frontend-framework migration.

## Handoff after Step 3

Document the released artifact names, supported OS versions/architectures, exact
runtime feature set, code-signing state, and any external-binary licenses.  The
next work should be release hardening (signing, update channel, crash diagnostics,
and support documentation), not a second desktop wrapper.
