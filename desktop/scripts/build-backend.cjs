"use strict";
const { spawnSync } = require("child_process");
const path = require("path");
const root = path.resolve(__dirname, "../..");
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const platformTarget = process.platform === "win32" ? "win32-x64" : "linux-x64";
const result = spawnSync(python, ["-m", "PyInstaller", "--noconfirm", "--distpath", path.join(root, "desktop"), path.join(root, "packaging", "organize-archive.spec")], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, ARCHIVE_TOOL_TARGET: process.env.ARCHIVE_TOOL_TARGET || platformTarget },
});
process.exit(result.status ?? 1);
