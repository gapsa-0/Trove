# Release process

Versions use SemVer and are declared once in `release-version.json`; public tags
are `v<version>`. The CI version gate requires Python, Electron, and the canonical
value to agree. After intentionally changing the canonical value, run
`npm run sync:version` from `desktop/`, review its four generated updates
(`organize_archive/__init__.py`, `pyproject.toml`, `desktop/package.json` and
`desktop/package-lock.json`), and commit them together. The lockfile is included
because `npm ci` does not verify its version field, so a stale value passes CI and
is then silently rewritten by whoever next runs `npm install`. Candidate builds are native CI artifacts, never developer uploads.

## Build inputs

A build has three staged inputs, each verified against a manifest so that the
same tag produces the same bytes:

| Input | Manifest | Staged by |
| --- | --- | --- |
| Python runtime | `packaging/requirements-desktop.txt` | `pip install -r` |
| Native tools (ffmpeg, ffprobe, ExifTool) | `packaging/tools/manifest.json` | `packaging/scripts/stage-tools.py --target <t>` |
| Bundled model weights | `packaging/models/manifest.json` | `packaging/scripts/stage-models.py` |

`npm run build:backend` refuses to run until the first two have produced their
`*-build-info.json` markers, so a build can never silently omit them.

The spec also carries an explicit `excludes` list. The app runs every model on
onnxruntime and never imports torch or transformers, but scikit-learn and SciPy
reach for torch through their `array_api_compat` shims — so on a machine that has
torch installed (any developer who has run `tools/dinov2_pet_export.py`),
PyInstaller would happily bundle ~700 MB of it. The exclusions keep the artifact
the same size whoever builds it.

Targets are `linux-x64` and `win32-x64`. ExifTool ships on Windows (a
self-contained executable plus its `exiftool_files/` runtime) but not on Linux,
where upstream distributes only a Perl source package; the Linux build therefore
declares it `unavailable` and metadata resolution falls back to Takeout sidecars,
filenames and filesystem timestamps.

### Model weights

Most weights are downloaded once at first run from a stable upstream URL and are
not packaged: the OpenCV Zoo YOLOX detector (~35 MB) and the InsightFace
`buffalo_l` pack (~184 MB). **A new installation therefore needs network access
once**, after which everything is local and offline. No media ever leaves the
machine — only the model downloads are network traffic.

The DINOv2 pet re-identification model is the exception: it is exported from a
Hugging Face checkpoint by `tools/dinov2_pet_export.py` (a dev-only tool needing
torch + transformers) and has no upstream URL, so a packaged build carries it
(~85 MB) and `organize_archive.runtime.bundled_model` prefers that copy.

`stage-models.py` takes it from a local `cache/models` directory (a developer
machine that already has it) or from the manifest `url`. CI runners have no
local copy, so they use the `url`, which points at a release asset on this
repository — the `models-v1` tag. `stage-models.py` downloads with no
authentication, which is why the asset has to be publicly reachable; that is
satisfied by this repository being public.

**Those releases are permanent.** The manifest pins the exact bytes by SHA-256,
so deleting or retagging an asset breaks reproducible builds of every version
that references it. Add a new tag (`models-v2`, …) instead of moving an old one.

### Publishing a new model asset

1. Stage or export the file locally, and confirm its SHA-256 matches the entry
   in `packaging/models/manifest.json` (or update the manifest if the weights
   genuinely changed — that means a new tag).
2. `gh release create <tag> <file> --repo gapsa-0/Trove
   --title "…" --notes "…provenance and sha256…"`.
3. Read the asset URL back with `gh release view <tag> --repo
   gapsa-0/Trove --json assets --jq '.assets[].url'` rather
   than assuming its shape, and record it as the manifest `url`.
4. Verify the CI path by wiping any local copy and forcing the download:
   `rm -rf packaging/models/staged && ARCHIVE_MODEL_SOURCE=/nonexistent
   HOME=/nonexistent python3 packaging/scripts/stage-models.py`. It must report
   `staged <name> from https://…`; the script re-verifies the hash after
   downloading and fails loudly on a mismatch.

A model with no local copy and no reachable `url` fails the build with an
explicit message rather than shipping a build whose Pets grouping cannot start.

## Required decisions before public beta

- Publisher identity: **not yet recorded**.
- Windows signing authority: **not yet configured**.
- Release host and supported Ubuntu/Debian versions: **not yet recorded**.
- Public-beta audience and feedback channel: **not yet recorded**.

Windows builds are published **unsigned**. The workflow used to demand a valid
Authenticode signature and abort without one, which meant no release could be cut at
all while signing was unconfigured; it now builds and ships the unsigned installer
that was being distributed anyway, and the README tells users how to get past the
SmartScreen warning that results.

Signing is still worth doing before a wider beta, with one caveat worth recording: an
ordinary OV certificate does not remove the SmartScreen warning, because reputation
accrues per publisher over download volume. Only EV-class signing earns immediate
trust. When a signing identity exists, set the `CSC_LINK` and `CSC_KEY_PASSWORD`
secrets and restore the two pieces removed from `.github/workflows/release.yml`: the
env entries on the `windows` job, and `--config.win.forceCodeSigning=true` plus an
`Get-AuthenticodeSignature` check after the build, so an unsigned artifact can never
be published as though it were signed.

Record the actual signing identity and supported platforms here before enabling the
protected `public-release` environment. To roll back, withdraw the affected release,
publish the prior known-good artifacts and checksums, and notify beta users. Send
security reports through the project's private maintainer contact once established.

## Clean-machine acceptance

Before publishing, record a run on a clean Windows x64 account and the selected
Ubuntu/Debian x64 release: install, native folder selection, small fixture indexing,
restart persistence, upgrade, and uninstall. Verify the Windows installer and installed
executable signatures; verify Linux executable permissions and AppImage/FUSE behaviour.
Confirm source media is untouched and explicitly record whether app data is retained.
