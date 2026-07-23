"use strict";
const { spawnSync } = require("child_process");
const path = require("path");
const root = path.resolve(__dirname, "../..");
const python = process.env.PYTHON || (process.platform === "win32" ? "python" : "python3");
const result = spawnSync(python, ["-m", "PyInstaller", "--noconfirm", "--distpath", path.join(root, "desktop"), path.join(root, "packaging", "organize-archive.spec")], { cwd: root, stdio: "inherit" });
process.exit(result.status ?? 1);
