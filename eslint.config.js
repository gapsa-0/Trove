"use strict";

// ESLint 9 flat config for the repo's JavaScript. Python is linted separately
// (ruff); this file only covers the Electron/Node desktop shell and the future
// browser-side GUI code.
//
// `desktop/` is the only npm project in the repo, so `@eslint/js` lives under
// `desktop/node_modules/`. Node's module resolution for a plain `require()` walks
// up from *this file's* directory (the repo root), which never reaches into a
// child directory -- so a bare `require("@eslint/js")` here would not find it.
// Resolve it explicitly instead.
const path = require("path");
const js = require(path.join(__dirname, "desktop", "node_modules", "@eslint/js"));

const sharedRules = {
  ...js.configs.recommended.rules,
  "no-undef": "error",
  "no-unused-vars": "warn",
};

module.exports = [
  {
    // `basePath` pins `files`/`ignores` resolution to the repo root regardless of
    // the caller's cwd. Flat config normally resolves relative glob patterns
    // against the directory that eslint.config.js was *auto-discovered* from --
    // but `desktop/package.json`'s `lint` script passes an explicit `--config`,
    // and ESLint's own loader only anchors patterns to the config file's
    // directory on auto-discovery; with an explicit `--config` it anchors them
    // to the process cwd instead. Without `basePath`, "desktop/src/**/*.cjs"
    // would need to be "src/**/*.cjs" when run from `desktop/` but
    // "desktop/src/**/*.cjs" when run from the repo root -- silently matching
    // zero files in one of the two cases. `basePath` removes that ambiguity.
    // The `lint` script now cd's to the repo root for a second reason: a path
    // argument outside the cwd (`../organize_archive/...`) is rejected outright,
    // so the browser modules cannot be linted from inside `desktop/` at all.
    basePath: __dirname,
    ignores: [
      "organize_archive/web/vendor/**",
      "desktop/node_modules/**",
      "desktop/release/**",
      "desktop/dist/**",
      "desktop/backend/**",
      "build/**",
    ],
  },
  {
    // Electron main-process / preload / build-script CommonJS files.
    basePath: __dirname,
    files: ["desktop/src/**/*.cjs", "desktop/scripts/**/*.cjs"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "commonjs",
      globals: {
        require: "readonly",
        module: "readonly",
        exports: "writable",
        process: "readonly",
        __dirname: "readonly",
        __filename: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        Buffer: "readonly",
        URL: "readonly",
        // Node 18+ (this repo targets Electron 37 / Node 22+): global fetch and
        // AbortSignal, used by src/main.cjs to health-check the spawned backend.
        fetch: "readonly",
        AbortSignal: "readonly",
      },
    },
    rules: sharedRules,
  },
  {
    // The browser-side app: native ES modules under organize_archive/web/static/js,
    // loaded by index.html with `<script type="module">`. There is no bundler and
    // no build step, so what eslint reads here is exactly what the browser runs.
    // `no-undef` is the rule that earns its keep: it is what catches a function
    // that moved to another module and is still called without an import, which
    // would otherwise surface only as a console error at click time.
    basePath: __dirname,
    files: ["organize_archive/web/static/js/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        window: "readonly",
        document: "readonly",
        fetch: "readonly",
        location: "readonly",
        console: "readonly",
        setTimeout: "readonly",
        localStorage: "readonly",
        navigator: "readonly",
        URL: "readonly",
        URLSearchParams: "readonly",
        Image: "readonly",
        IntersectionObserver: "readonly",
        history: "readonly",
        setInterval: "readonly",
        clearInterval: "readonly",
        clearTimeout: "readonly",
        requestAnimationFrame: "readonly",
        requestIdleCallback: "readonly",
        getSelection: "readonly",
        Node: "readonly",
        alert: "readonly",
        confirm: "readonly",
        prompt: "readonly",
        // Leaflet, loaded as a classic script from vendor/ ahead of the module,
        // so it is a real global rather than something a module can import.
        L: "readonly",
      },
    },
    rules: sharedRules,
  },
];
