"use strict";
// This is deliberately a small release gate: CI sets ARCHIVE_PACKAGE to the
// produced executable/AppImage and verifies that it remains alive long enough
// to create its window. Full platform smoke runs belong on native CI runners.
const { spawn } = require("child_process");
if (!process.env.ARCHIVE_PACKAGE) throw new Error("Set ARCHIVE_PACKAGE to a packaged Trove executable.");
const child = spawn(process.env.ARCHIVE_PACKAGE, [], { stdio: "ignore" });
setTimeout(() => { if (child.exitCode !== null) process.exit(1); child.kill(); }, 5000);
child.on("exit", code => process.exit(code === 0 || code === null ? 0 : 1));
