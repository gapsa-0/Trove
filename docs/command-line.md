# Command line and development

Everything in this document is for running Trove from a source checkout or
building the desktop packages yourself. If you just want to use Trove, the
[README](../README.md) install instructions are all you need.

## Command line and source setup

Trove requires Python 3.13 or newer. From a clone, `make setup` builds the
virtualenv and installs every extra at the versions the project is tested against:

```bash
make setup          # add PYTHON=/path/to/python3.13 if there is no system one
```

To do it by hand, or to install only the extras for the features you want:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[cli,media,faces,pets,semantic]' -c constraints.txt
```

`-c constraints.txt` is what pins those extras to the tested versions; without it
pip is free to resolve anything satisfying the open ranges in `pyproject.toml`.
[dependencies](dev/dependencies.md) explains what each extra enables and what
degrades without it.

`exiftool` and `ffmpeg`/`ffprobe` are recommended system tools. The core scanner works
without them, with reduced embedded-metadata and video support.

The CLI exposes the same durable catalogue operations:

```bash
oa config --add-root /path/to/archive
oa config --set-timezone America/Argentina/Buenos_Aires
oa init
oa scan
oa enrich
oa dedup
oa pets
oa faces
oa gui
```

Useful companion commands are `oa status`, `oa dates`, `oa config --show`,
`oa logs` (the last 200 log lines; `--path` prints just the file's location), and
`oa migrate-data` for copying an older project-local `data/` directory into the
per-user data location. All long commands are designed to be re-run.

Face extraction rejects low-confidence, tiny, blurry, severely over/underexposed,
and substantially clipped candidates before they enter People. Inspect persisted
decision counts and post-clustering unassigned noise with
`oa faces --quality-report`, or test the configured thresholds against up to 100
pending images without changing the catalog:

```bash
oa faces --calibrate 100
```

`oa gui` starts the local interface at `http://127.0.0.1:8756/`; it opens a standalone
browser window when a supported Chromium-family browser is available. Use `--tab` to
open a normal tab or `--no-open` when launching it remotely.

The native "choose folder" dialog is provided by the desktop app; in a plain browser
(`oa gui`) there is no OS picker, so **type the absolute folder path** into the box on
the welcome screen (e.g. `/mnt/photos/Multimedia` or `D:\Photos`) and press *Choose media
folder*. Use the packaged desktop app if you want to click through a folder dialog.


## Build the desktop app

The Electron shell owns the native window and folder picker; the Python backend stays
loopback-only. From `desktop/`:

```bash
npm install
npm run dev
npm run build:backend
npm run package:linux
npm run package:win
```

Packaging first needs its two staged inputs, which are downloaded and SHA-256
verified against manifests in `packaging/` rather than committed:

```bash
python3 packaging/scripts/stage-tools.py --target linux-x64   # or win32-x64
python3 packaging/scripts/stage-models.py
```

`npm run build:backend` refuses to run until both have been staged, so a build
cannot silently ship without ffmpeg/ffprobe or without the bundled pet re-ID
model. See [the release guide](docs/release.md#build-inputs) for what each input
is and where it comes from.

**`npm run dev` must reach the project virtualenv.** In development the shell launches
the backend with plain `python3`. If that interpreter is the system Python rather than
the project `.venv`, OpenCV and onnxruntime are missing and **Pets and People report
"unavailable"** (the rest of the app still works). Activate the venv first, or point the
shell at it explicitly:

```bash
# either activate the venv before launching
. ../.venv/bin/activate && npm run dev
# …or name the interpreter for this run
PYTHON=../.venv/bin/python npm run dev   # Windows: set PYTHON=..\.venv\Scripts\python.exe
```

Packaged builds are unaffected — they bundle their own interpreter and models.

Native-tool staging and packaging files are in `packaging/`. Release versioning and
clean-machine checks are described in [the release guide](docs/release.md).
