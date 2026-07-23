"use strict";

// Keep release-version.json as the one human-edited release version.  The three
// runtime manifests are checked in so source installs continue to work offline.
const fs = require("fs");
const path = require("path");
const root = path.resolve(__dirname, "../..");
const release = JSON.parse(fs.readFileSync(path.join(root, "release-version.json")));
const write = process.argv.includes("--write");
const pythonPath = path.join(root, "organize_archive", "__init__.py");
const pyprojectPath = path.join(root, "pyproject.toml");
const packagePath = path.join(root, "desktop", "package.json");
if (write) {
  fs.writeFileSync(pythonPath, fs.readFileSync(pythonPath, "utf8").replace(/__version__\s*=\s*["'][^"']+["']/, `__version__ = "${release.version}"`));
  fs.writeFileSync(pyprojectPath, fs.readFileSync(pyprojectPath, "utf8").replace(/^version\s*=\s*["'][^"']+["']/m, `version = "${release.version}"`));
  const packageJson = JSON.parse(fs.readFileSync(packagePath)); packageJson.version = release.version;
  fs.writeFileSync(packagePath, `${JSON.stringify(packageJson, null, 2)}\n`);
}
const python = fs.readFileSync(pythonPath, "utf8").match(/__version__\s*=\s*["']([^"']+)["']/)?.[1];
const pyproject = fs.readFileSync(pyprojectPath, "utf8").match(/^version\s*=\s*["']([^"']+)["']/m)?.[1];
const electron = JSON.parse(fs.readFileSync(packagePath)).version;
if (!release.version || !/^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(release.version)) {
  throw new Error("release-version.json must contain a SemVer version");
}
const values = { python, pyproject, electron };
const bad = Object.entries(values).filter(([, value]) => value !== release.version);
if (bad.length) throw new Error(`Release version ${release.version} disagrees: ${bad.map(([k, v]) => `${k}=${v}`).join(", ")}`);
console.log(`Release version OK: ${release.version}`);
