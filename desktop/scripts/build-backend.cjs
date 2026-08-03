"use strict";
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "../..");
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const platformTarget = process.platform === "win32" ? "win32-x64" : "linux-x64";
const target = process.env.ARCHIVE_TOOL_TARGET || platformTarget;

// Staging is a separate, hash-verified step (packaging/scripts/stage-tools.py).
// Check its output up front: a build that silently omits the native tools still
// packages fine, and only fails in the user's hands. Model weights are not checked
// here because they are no longer bundled -- the app fetches them on first use.
for (const [what, marker, fix] of [
  ["native tools", path.join(root, "packaging", "tools", "staged", target, "tools-build-info.json"),
    `python3 packaging/scripts/stage-tools.py --target ${target}`],
]) {
  if (!fs.existsSync(marker)) {
    console.error(`error: ${what} are not staged for ${target}.\n  Run: ${fix}`);
    process.exit(1);
  }
}

// desktop/build/ is gitignored (it matches the repo-wide `build/` rule), so the
// app icons electron-builder needs are never in a fresh clone. Render them from
// the one canonical mark instead of committing binaries that can drift from it.
const icons = spawnSync(python, [path.join(root, "tools", "build", "render_logo.py")],
  { cwd: root, stdio: "inherit" });
if (icons.status !== 0) {
  console.error("error: could not render the app icons (tools/build/render_logo.py).\n"
    + "  It needs Pillow, so point PYTHON at the virtualenv you build with:\n"
    + "    PYTHON=.venv/bin/python npm run build:backend");
  process.exit(icons.status ?? 1);
}

const result = spawnSync(python, ["-m", "PyInstaller", "--noconfirm", "--distpath", path.join(root, "desktop"), path.join(root, "packaging", "organize-archive.spec")], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, ARCHIVE_TOOL_TARGET: target },
});
process.exit(result.status ?? 1);
