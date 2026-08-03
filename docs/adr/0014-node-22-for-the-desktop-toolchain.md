# 0014. The desktop toolchain is pinned to Node 22

- **Status:** Accepted
- **Date:** 2026-08-02

## Context

Nothing in the repository stated which Node version builds the desktop app.
CI has always pinned `node-version: 22` in every workflow, but a checkout
carried no `.nvmrc`, no `engines` field, and no mention of Node in the README,
CONTRIBUTING, or `docs/command-line.md`. A developer used whatever their system
had.

On Node 26 that silently produces a broken checkout. Electron's `postinstall`
unpacks the runtime from a cached zip with `extract-zip` 2.0.1 (via
`yauzl` 2.10.0, both unmaintained). On Node 26 the extraction writes the first
entry of the archive, then hangs: the promise never settles, so Node's event
loop drains, the process exits 0, and `npm ci` records a clean install. There is
no binary, no error, and no non-zero exit anywhere. `make setup` and `make check`
both pass, and the failure surfaces much later as `electron .` throwing
"Electron failed to install correctly, please delete node_modules/electron and
try installing again" — advice that does not help, because a reinstall repeats
the same silent no-op.

The download is not the problem: the cached zip is complete and its SHA-256
matches Electron's `checksums.json`, and `yauzl` on its own reads every entry on
Node 26. Nor is it npm's install-script policy — `allowScripts` is advisory in
npm 11.17, and the script does run. It is purely a Node version incompatibility
in a transitive dependency the project does not control.

## Decision

Node 22 is the version the desktop toolchain is built and tested on, pinned in
three places that each do a different job:

- `.nvmrc` — `22`. What `fnm`/`nvm`/`asdf` select on entering the directory, and
  the number the docs and CI are written against. It sits at the repository root
  rather than beside `desktop/package.json` because `make setup` runs `npm ci`
  from the root and version managers search upwards, so one file at the top
  covers both.
- `desktop/package.json` — `"engines": { "node": ">=20 <23" }`. The 20 and 22 LTS
  lines predate the breakage; the upper bound is what keeps Node 26 out.
- `.github/workflows/*.yml` — `node-version: 22`, as before.

`desktop/.npmrc` sets `engine-strict=true`, which is what makes `engines`
load-bearing: without it npm prints `EBADENGINE` and installs anyway, which is
the outcome this record exists to prevent. On an unsupported Node, `npm ci` now
fails outright.

Only Node 22 is actually exercised. The `>=20` bound is a courtesy to
contributors on the previous LTS, not a tested claim, and there is no
compatibility matrix.

## Consequences

- A contributor building the desktop app needs Node 20 or 22; `npm ci` refuses
  anything else rather than producing an app that fails at launch.
- Moving to a newer Node is a deliberate act — raise `.nvmrc`, `engines`, and the
  workflows together, and confirm the Electron postinstall still unpacks. It is
  not a version-string edit, since the thing that breaks is invisible to every
  other check.
- End users of the packaged builds are unaffected: the Electron runtime is
  bundled and no Node is involved on their machine.
- The pin is enforced, not just asserted here: `tests/unit/test_node_version.py`
  reads `.nvmrc` and checks `engines.node`, `engine-strict`, and every workflow's
  `node-version` against it, in the same shape as
  `tests/unit/test_python_version.py` does for ADR 0007's Python floor.
- Two guards cover the failure mode itself rather than its cause, because
  "install script exits 0 without installing" is what made this expensive:
  `make setup` fails if `desktop/node_modules/electron/path.txt` is missing after
  `npm ci`, and CI's electron job asserts the same file.
