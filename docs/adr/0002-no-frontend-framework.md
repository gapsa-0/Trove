# 0002. No frontend framework, no bundler

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

The GUI is a local, single-user desktop application served from a Python
process on `127.0.0.1`. It needs a browsable interface — timeline, media
grid, folder view, people/pets, places, duplicates — but has none of the
concerns that usually justify a modern frontend toolchain: no multi-team
codebase, no server-side rendering requirement, no need to target browsers
that lack native ES module support (the app ships its own Chromium via
Electron).

## Decision

The web UI is vanilla ES modules served as static files. `organize_archive/web/static/js/`
holds about twenty modules and `organize_archive/web/static/css/` about
nineteen stylesheets; `organize_archive/web/index.html` is a thin shell that
loads `main.js` with `<script type="module">`. There is no React, Vue, or
Svelte, and no bundler (no webpack, Vite, or Rollup) or build step for the
frontend at all — `find . -name package.json` outside `node_modules` turns up
only `desktop/package.json`, the Electron shell's own npm project, confirming
there is no separate frontend build tooling anywhere in the repository.

Because nothing bundles the JavaScript, deployment of the frontend is a plain
file copy: `pyproject.toml`'s `[tool.setuptools.package-data]` lists
`"organize_archive.web" = ["*.html", "vendor/*", "static/css/*", "static/js/*", "*.png"]`,
so the packaged desktop build ships the directory exactly as it sits in the
repository, and the browser loads `import` paths as real URLs against that
same directory rather than against a generated bundle.

`eslint.config.js` lints `organize_archive/web/static/js/**/*.js` as native
ES modules (`sourceType: "module"`) with an explicit browser-globals block
(`window`, `document`, `fetch`, `Image`, `IntersectionObserver`, Leaflet's
`L` global loaded as a classic script ahead of the module, and so on) and
`no-undef` set to `"error"`. The config's own comment explains why that rule
in particular earns its keep here: "it is what catches a function that moved
to another module and is still called without an import, which would
otherwise surface only as a console error at click time" — exactly the kind
of mistake a bundler's module resolution would normally catch for you.

That absence of a bundler creates one specific gap eslint cannot close:
inline `on*` handler attributes (`onclick="showSection('library')"`, both in
`index.html` and inside the template-literal markup the screens generate) are
evaluated by the browser against the global `window` object, not against
`main.js`'s local scope — so a function only exported from its own module is
invisible to them. `main.js` therefore ends with an explicit
`Object.assign(window, {...})` block naming every function an inline handler
calls, and `tools/dev/check_handlers.py` is the CI check for exactly that: it
scans `index.html` and every screen module for `on*="..."` attributes, scans
`main.js`'s export block, and fails (exit 1) if the two lists disagree in
either direction. It runs in `.github/workflows/ci.yml` and in the local
`make check` target (`Makefile`). Its own module docstring states the failure
mode this exists to catch: "the page renders perfectly and the button does
nothing, with no error until someone clicks it."

## Consequences

- No `npm install`, `npm run build`, or Node toolchain sits in the Python
  build or release path; the frontend is exactly the files under
  `organize_archive/web/`.
- The browser's own module resolution is the only "bundler" in play, so an
  `import` path bug is a load-time 404 in the browser console, not a build
  failure — `no-undef` and `check_handlers.py` exist specifically to catch
  the classes of mistake a bundler would otherwise catch automatically.
- Cost: no tree-shaking and no minification, so every module ships in full
  and unminified. For a purely local, single-user app this has not mattered
  enough to trade away the simplicity of a build-free frontend.
- Any new inline handler must be added to both sides — the markup and
  `main.js`'s `Object.assign(window, {...})` block — or CI fails.
