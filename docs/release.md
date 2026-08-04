# Release process

Versions use SemVer and are declared once in `release-version.json`; public tags
are `v<version>`. The CI version gate requires Python, Electron, and the canonical
value to agree. After intentionally changing the canonical value, run
`npm run sync:version` from `desktop/`, review its four generated updates
(`trove/__init__.py`, `pyproject.toml`, `desktop/package.json` and
`desktop/package-lock.json`), and commit them together. The lockfile is included
because `npm ci` does not verify its version field, so a stale value passes CI and
is then silently rewritten by whoever next runs `npm install`. Candidate builds are native CI artifacts, never developer uploads.

## Release checklist

In order. Each step is here because skipping it has produced, or would produce,
a release that is wrong in a way nobody notices until it is published.

1. **Move `CHANGELOG.md`'s `[Unreleased]` section into a new dated version
   section.** That section's body is also the GitHub release's body: paste it in
   as-is when creating the release. Do this before the version bump, so the
   section heading and the canonical version cannot disagree.
2. **Bump the version**: change `release-version.json`, run `npm run sync:version`
   from `desktop/`, review its four generated updates and commit them together.
3. **`make check`** — the full gate, not `make test`.
4. **Skim the size allowlist** in `tools/dev/check_sizes.py`: did it grow since
   the last release, and does each new entry have the reason its commit body
   promised? An allowlist that only ever grows is a ratchet running backwards.
5. **Build and launch the packaged app on both Linux and Windows.** A green suite
   says nothing about package data — a missing directory under
   `trove/web/` produces a build that imports fine and serves a blank
   page. Launch it and open a screen.
6. **Build from a clean tree, not the working copy.** `build/lib/` is a stale
   setuptools staging directory that is never cleaned between builds, so
   setuptools copies the current tree in *beside* the old one and ships both — a
   local wheel has already been observed carrying a package that had been renamed
   two commits earlier. Either `make clean` first, or export and build
   elsewhere:

   ```
   git ls-files -z | tar --null -T - -cf - | (mkdir -p /tmp/rel && cd /tmp/rel && tar xf -)
   ```

   PyInstaller is unaffected (it resolves the package path to the live source),
   but "I built a wheel and it looked right" is not trustworthy without this.
7. **Record the clean-machine acceptance run** — see below.

## Build inputs

A build has two staged inputs, each verified against a manifest so that the
same tag produces the same bytes:

| Input | Manifest | Staged by |
| --- | --- | --- |
| Python runtime | `packaging/requirements-desktop.txt` | `pip install -r` |
| Native tools (ffmpeg, ffprobe, ExifTool) | `packaging/tools/manifest.json` | `packaging/scripts/stage-tools.py --target <t>` |

`npm run build:backend` refuses to run until the native tools have produced their
`tools-build-info.json` marker, so a build can never silently omit them. Model
weights are not a build input at all — see below.

FFmpeg is staged from BtbN's **shared** build rather than the static one: two
small executables plus the `libav*` libraries they share, instead of two binaries
that each embed the entire codec set. That is 162 MB instead of 266 MB on Linux,
151 MB instead of 263 MB on Windows. `stage-tools.py` copies those libraries flat
beside the executables (the `runtime_libs` manifest field), preserving the soname
symlinks — dereferencing them would stage `libavcodec` twice and give the saving
straight back. Windows needs nothing more, since the loader searches the `.exe`'s
own directory; Linux needs `LD_LIBRARY_PATH`, which
`trove.runtime.tool_env` supplies at every spawn. Upstream's RPATH is
`-Wl:../lib`, a quoting bug in their link flags rather than `$ORIGIN/../lib`, so
it cannot be relied on.

The spec also carries an explicit `excludes` list. The app runs every model on
onnxruntime and never imports torch or transformers, but scikit-learn and SciPy
reach for torch through their `array_api_compat` shims — so on a machine that has
torch installed (any developer who has run `tools/build/dinov2_pet_export.py`),
PyInstaller would happily bundle ~700 MB of it. The exclusions keep the artifact
the same size whoever builds it.

Targets are `linux-x64` and `win32-x64`. ExifTool ships on Windows (a
self-contained executable plus its `exiftool_files/` runtime) but not on Linux,
where upstream distributes only a Perl source package; the Linux build therefore
declares it `unavailable` and metadata resolution falls back to Takeout sidecars,
filenames and filesystem timestamps.

### Model weights

**No model weights are packaged.** Every one of them is downloaded once, on the
first run of the feature that needs it, and verified before use. **A new
installation therefore needs network access once**, after which everything is
local and offline. No media ever leaves the machine — only the model downloads are
network traffic.

Most come from a stable upstream URL: the OpenCV Zoo YOLOX detector (~35 MB) and
the InsightFace `buffalo_l` pack (~184 MB). Two do not exist upstream in ONNX form
at all — the AdaFace embedder (~249 MB) and the DINOv2 pet re-identification model
(~84 MB), both exported from Hugging Face checkpoints by dev-only tools that need
torch + transformers. Those two are re-published as release assets on this
repository (the `models-v1` tag), and `trove/model_manifest.py`
resolves them the same way as the rest.

They used to travel inside the installer, which cost 349 MB of every download —
for files most users would fetch over the same connection anyway. Removing them is
the single largest reason the installers roughly halved. `tests/unit/test_no_bundled_models.py`
fails the build if the spec starts bundling them again.

`packaging/scripts/stage-models.py` survives as a developer convenience: it fills
`packaging/models/staged/`, which is the second tier of the resolver, so a checkout
can run the model-backed tests offline. CI runs only its `--validate` mode.

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

A model with no local copy and no reachable `url` fails with an explicit message
naming the export tool, raised before anything is downloaded — see
`tests/unit/test_detect_preflight.py`.

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
