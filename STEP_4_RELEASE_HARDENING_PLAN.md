# Step 4 — Release hardening and public-beta delivery

## Goal

Turn the Step 3 desktop build into a release process users can trust and you can
support.  The outcome is a repeatable, native Windows/Linux public-beta release
with signed Windows binaries, integrity information, actionable local diagnostics,
and a short install/support guide.

This step is release engineering.  It does not add new archive features, cloud
accounts, telemetry, or automatic updates.

## Current baseline (verified)

The completed Step 3 implementation already provides:

- Electron 37 + electron-builder 26, with a locked `desktop/package-lock.json`.
- A loopback-only Python sidecar with a machine-readable `READY` line and
  `/api/health` endpoint.
- A secure BrowserWindow, a minimal native folder-picker bridge, and local startup
  diagnostic capture.
- PyInstaller one-directory packaging, plus Windows NSIS and Linux AppImage/`.deb`
  targets.
- A pinned Python desktop dependency list and a backend readiness/shutdown test.

The major release gaps are: no CI workflow, no actual platform tool payloads,
no release signing configuration, no clean-machine install test, no source-of-truth
for the duplicated version values, and no user-facing support/recovery guide.

## Release principles

1. **Local first remains literal.** No crash report, log, archive path, photo,
   database, or telemetry leaves the device automatically.
2. **A release is reproducible.** Every public binary comes from a tagged commit on
   a native CI runner, not a developer's unrecorded workstation build.
3. **A release is identifiable.** Users can see its version, verify its checksum,
   and report it with a local diagnostics bundle.
4. **A failed release fails closed.** A production Windows build without a valid
   signature must fail—not silently ship unsigned.
5. **User data is never an installer payload.** Updating or uninstalling Archive
   must never alter source media or silently remove the per-user catalogue.

## Decision gate: signing authority and release host

Before implementing the production-release workflow, choose and record these
values in `docs/release.md`:

| Decision | Required choice |
| --- | --- |
| Publisher identity | Legal/public name shown in the Windows signature and support docs |
| Windows signing | Azure Trusted Signing, OV certificate, or EV certificate |
| Release host | GitHub Releases or another HTTPS download host |
| First supported Linux | exact Ubuntu/Debian version(s) for `.deb` validation; AppImage remains best-effort x64 |
| Public-beta policy | who receives builds, how feedback is reported, and whether unsigned internal builds are permitted |

Recommendation: use **GitHub Releases** for the first public beta and choose a
Windows signing method that works with the intended build environment.  Azure Trusted
Signing is CI-friendly; a conventional certificate can build reputation over time;
an EV certificate can establish trust immediately but is generally tied to hardware.
Electron-builder supports certificate credentials through environment variables and
can enforce signing with `forceCodeSigning`. [Electron-builder code-signing
documentation](https://www.electron.build/docs/features/code-signing/)

Do not put a certificate, private key, signing password, GitHub token, or external
tool license key in the repository, a `.env` file that might be committed, or a
release artifact.

## Implementation plan

### 1. Establish a single release version

The current Python package, Electron package, and sidecar health response all carry
`0.1.0` separately. Make the release version one controlled value.

- Adopt SemVer and release tags such as `v0.1.0-beta.1`.
- Keep the canonical version in one small repository file or release script.
- Generate/update `organize_archive.__version__`, `pyproject.toml`, and
  `desktop/package.json` from it before a release build; CI must fail if the values
  disagree.
- Include the version and Git commit SHA in the desktop sidecar `READY` record and
  `/api/health` response.
- Add an in-app **Archive → About** panel showing product version, commit, backend
  version, app-data directory, and the externally bundled tool versions—without
  exposing user archive paths by default.

### 2. Make external tools a verified release input

`packaging/tools/` is currently a handoff directory, not a reproducible
platform-specific payload. Replace it with an explicit manifest and fetch/verify
step.

- Add `packaging/tools/manifest.json` with, per platform/architecture: tool name,
  upstream URL, version, SHA-256, license, and expected executable name.
- Add a script that downloads only the current runner's tool archive, verifies its
  SHA-256 before extraction, and writes it to a platform-specific staging directory.
- Package only that staging directory. Never copy a mixed Windows/Linux tool tree
  into PyInstaller `datas`.
- At build time run `exiftool -ver`, `ffprobe -version`, and `ffmpeg -version` from
  the staged files; fail if they do not execute.
- Emit a third-party notices file in each release containing licenses for Electron,
  Python runtime, PyInstaller, FFmpeg, ExifTool, and bundled Python packages.

Confirm the licensing/distribution terms for each selected FFmpeg and ExifTool build
before public distribution; this is a release blocker, not a README footnote.

### 3. Strengthen local diagnostics and failure recovery

Retain the existing startup-error dialog, but make diagnostics usable without
collecting data remotely.

- Replace the single overwritten backend error file with small rotating logs:
  Electron main process, Python backend stderr, and a release/build-info file.
- Add a user-triggered **Copy diagnostics** action in the About/Settings screen.
  It copies version, OS version, tool availability, and recent error lines; it must
  omit catalogue content, media paths, filenames, people labels, and API keys.
- Add a user-triggered **Open data folder** action with explicit copy explaining
  that it contains the catalogue/cache, not originals. This must open a fixed
  known directory—never a path supplied by web content.
- Add a startup recovery option only when necessary: “Start with extensions
  disabled” is out of scope because there are no extensions; instead offer “Open
  diagnostics” and “Quit.”
- In release builds, make the PyInstaller sidecar a windowless executable on
  Windows. Keep all diagnostics in files/stderr captured by Electron so users never
  see a transient console window.
- Register Electron `uncaughtException`/`unhandledRejection` handling that writes a
  local diagnostic record then presents a concise restart/quit dialog. Do not hide
  errors or attempt infinite automatic relaunch loops.

### 4. Build native CI workflows

Add separate workflows under `.github/workflows/`.

#### Continuous integration (`ci.yml`)

Run on pull requests and pushes to the main development branch:

- Linux: install Python 3.13, run `pytest -q`, and run a sidecar readiness/health
  test with isolated XDG data.
- Node: run `npm ci` in `desktop/`, then validate static Electron configuration and
  the ready-line parser tests.
- Add a `--help`/tool-manifest validation without downloading full production tools.
- Upload logs/test reports only on failure, with a short retention period.

#### Release candidate (`release-candidate.yml`)

Run manually for a tag/commit and create non-public artifacts:

- Native `windows-latest` job builds the backend, stages/verifies Windows tools,
  creates the NSIS installer, and performs a packaged smoke launch.
- Native `ubuntu-latest` job stages/verifies Linux tools, creates AppImage and
  `.deb`, and performs packaged smoke tests.
- Produce SHA-256 checksum files and a machine-readable `release-manifest.json`
  containing commit, versions, artifact names/sizes/hashes, and tool versions.
- Upload all candidates as CI artifacts for review. Do not publish or sign an RC by
  default.

#### Public release (`release.yml`)

Trigger only from a protected, reviewed `v*` tag:

- Repeat native builds; do not promote a developer-built file.
- Enable production signing and set electron-builder `forceCodeSigning: true` for
  the Windows job. This is important because electron-builder otherwise can continue
  with unsigned output when credentials are absent. [Code-signing behavior](https://www.electron.build/docs/features/code-signing/)
- Sign the NSIS installer and all relevant executables; verify signatures before
  upload with Windows tooling.
- Create checksums, notices, and release manifest; attach all to the draft release.
- Use the release host token only in this protected workflow with minimum required
  permissions. For GitHub Releases, grant `contents: write` only to the publishing
  job.
- Attach build provenance/attestation where the selected host/repository plan
  supports it. GitHub Actions can create attestations that establish binary build
  provenance. [GitHub artifact-attestation documentation](https://docs.github.com/en/enterprise-cloud%40latest/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)

Use `npm ci`, not `npm install`, in all CI workflows. Keep Python and Node cache keys
tied to their lock/requirements files. Pin action versions by full commit SHA once
the workflow is ready for public releases.

### 5. Test installation and lifecycle on clean machines

The existing smoke test only establishes that a packaged build stays alive long
enough to create a window. Add a release acceptance checklist that runs on clean
OS images, not the developer workstation.

#### Windows x64

1. Verify Authenticode signatures for installer and installed executable.
2. Install as a standard non-admin user; validate publisher, icon, Start-menu and
   desktop shortcut.
3. Start Archive, use the native folder picker, index a small fixture archive, close,
   reopen, and confirm persistence in `%LOCALAPPDATA%`.
4. Upgrade from the prior public-beta installer and confirm catalogue/roots persist.
5. Uninstall and verify the binaries/shortcuts are removed but source media is
   untouched; clearly record whether application data is retained or offer an
   explicit checkbox to remove it.

#### Linux x64

1. On the chosen Ubuntu/Debian version, install the `.deb`, launch from the app menu,
   and validate the native folder picker, persistence in XDG data, and clean exit.
2. Test the AppImage on a second supported distribution; document exact FUSE/runtime
   prerequisites and the fallback command if required.
3. Upgrade/reinstall without losing catalogue state.
4. Confirm all bundled executables carry execute permissions after packaging.

Use a small generated fixture archive for these tests. It must contain only
non-sensitive test media and representative image, video, audio, document, HEIF,
duplicate, missing-metadata, and nested-folder cases.

### 6. Publish support documentation

Add concise, non-technical documents linked from the README and release notes:

- `docs/install-windows.md`: download, signature/publisher expectation, install,
  first folder selection, uninstall/data-retention behavior.
- `docs/install-linux.md`: choose `.deb` or AppImage, exact install/run commands,
  desktop integration expectations, and FUSE troubleshooting.
- `docs/privacy-and-data.md`: exact location of app data by OS; confirmation that
  originals are never moved/renamed/edited/deleted; what face models and cache do;
  what optional semantic search does and why it is off by default.
- `docs/troubleshooting.md`: backend cannot start, missing tool, locked database,
  unreachable folder, corrupted cache, diagnostic-copy instructions, and safe
  migration/backup guidance.
- `docs/release.md`: versioning, signing identity, supported platforms, release
  checklist, rollback procedure, and security-contact process.

Make it clear that the catalogue database and cache are valuable derived data. Give
users an explicit, simple backup recommendation—copy the app-data directory while
Archive is closed—and a restore procedure. Do not implement automated backup in this
step.

## Release checklist

Before making a release public, all must be true:

- [ ] Tag, Python, Electron, sidecar, and About versions agree.
- [ ] Windows and Linux builds ran on their respective native CI runners.
- [ ] Bundled tools match verified manifest hashes and run from the packaged app.
- [ ] `pytest -q`, packaged smoke tests, and clean-machine acceptance tests pass.
- [ ] Windows installer/executables are signed and signature verification passes.
- [ ] Checksums, notices, release manifest, install guides, and known limitations are attached.
- [ ] No user data, media, secrets, or developer-local paths are in artifacts/logs.
- [ ] Upgrade and uninstall behavior have been tested and documented.
- [ ] A rollback plan exists: pull the release, mark it withdrawn, publish the
  previous known-good checksum/artifacts, and communicate the affected versions.

## Definition of done

- A protected tag can produce reviewed, native Windows/Linux candidate artifacts
  consistently.
- Public Windows releases fail if signing is unavailable and verify as signed after
  build.
- Every artifact carries a checksum, release manifest, notices, and clear install
  path.
- A user can diagnose a startup problem locally without exposing private archive
  data.
- Clean-machine install, upgrade, persistence, and uninstall behavior are verified
  for the declared supported platforms.
- Release and recovery documentation is sufficient for someone who did not build the
  project to install and use Archive safely.

## Explicitly out of scope

- Automatic updates and a release feed. This should be a later step after signed
  public releases have proven stable. Electron-updater supports NSIS/AppImage/DEB,
  but it requires a deliberate hosting, verification, rollback, and user-consent
  design. [Electron auto-update documentation](https://www.electron.build/docs/features/auto-update/)
- macOS packaging/notarization.
- Cloud backup, remote support, telemetry, analytics, or automatic crash uploads.
- New media-analysis or browsing features.
