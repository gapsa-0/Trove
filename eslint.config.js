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
    // but `desktop/package.json`'s `lint` script passes an explicit
    // `--config ../eslint.config.js`, and ESLint's own loader only anchors
    // patterns to the config file's directory on auto-discovery; with an
    // explicit `--config` it anchors them to the process cwd instead (`desktop/`
    // here). Without `basePath`, "desktop/src/**/*.cjs" would need to be
    // "src/**/*.cjs" when run via `npm run lint` from `desktop/`, but
    // "desktop/src/**/*.cjs" when run from the repo root -- silently matching
    // zero files in one of the two cases. `basePath` removes that ambiguity.
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
    // organize_archive/web/static/js/** does not exist yet: Stage 10 of the repo
    // overhaul creates it by splitting the current 9,000-line index.html into
    // real modules. This block is declared ahead of time so Stage 10 only has to
    // add this glob to the `lint` script in desktop/package.json; the rules and
    // globals are already in place. Today the glob simply matches nothing.
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
      },
    },
    rules: sharedRules,
  },
];
