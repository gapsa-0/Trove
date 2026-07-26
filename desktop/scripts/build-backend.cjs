"use strict";
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "../..");
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const platformTarget = process.platform === "win32" ? "win32-x64" : "linux-x64";
const target = process.env.ARCHIVE_TOOL_TARGET || platformTarget;

// Staging is a separate, hash-verified step (packaging/scripts/stage-*.py). Check
// its output up front: a build that silently omits the native tools or the bundled
// model weights still packages fine, and only fails in the user's hands.
for (const [what, marker, fix] of [
  ["native tools", path.join(root, "packaging", "tools", "staged", target, "tools-build-info.json"),
    `python3 packaging/scripts/stage-tools.py --target ${target}`],
  ["model weights", path.join(root, "packaging", "models", "staged", "models-build-info.json"),
    "python3 packaging/scripts/stage-models.py"],
]) {
  if (!fs.existsSync(marker)) {
    console.error(`error: ${what} are not staged for ${target}.\n  Run: ${fix}`);
    process.exit(1);
  }
}

const result = spawnSync(python, ["-m", "PyInstaller", "--noconfirm", "--distpath", path.join(root, "desktop"), path.join(root, "packaging", "organize-archive.spec")], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, ARCHIVE_TOOL_TARGET: target },
});
process.exit(result.status ?? 1);
