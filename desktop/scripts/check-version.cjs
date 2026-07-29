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
const lockPath = path.join(root, "desktop", "package-lock.json");
// npm mirrors package.json's version in two places in the lockfile: the root
// object and the "" self-entry under packages.  `npm ci` does not verify either,
// so a stale value survives CI and is then silently rewritten by whoever next
// runs `npm install` -- landing an unrelated version diff in their commit, which
// is exactly what happened to 0.1.1.  Sync it here instead.
const lockVersions = (lock) => [lock.version, lock.packages?.[""]?.version];
if (write) {
  fs.writeFileSync(pythonPath, fs.readFileSync(pythonPath, "utf8").replace(/__version__\s*=\s*["'][^"']+["']/, `__version__ = "${release.version}"`));
  fs.writeFileSync(pyprojectPath, fs.readFileSync(pyprojectPath, "utf8").replace(/^version\s*=\s*["'][^"']+["']/m, `version = "${release.version}"`));
  const packageJson = JSON.parse(fs.readFileSync(packagePath)); packageJson.version = release.version;
  fs.writeFileSync(packagePath, `${JSON.stringify(packageJson, null, 2)}\n`);
  const lockJson = JSON.parse(fs.readFileSync(lockPath));
  lockJson.version = release.version;
  if (lockJson.packages?.[""]) lockJson.packages[""].version = release.version;
  fs.writeFileSync(lockPath, `${JSON.stringify(lockJson, null, 2)}\n`);
}
const python = fs.readFileSync(pythonPath, "utf8").match(/__version__\s*=\s*["']([^"']+)["']/)?.[1];
const pyproject = fs.readFileSync(pyprojectPath, "utf8").match(/^version\s*=\s*["']([^"']+)["']/m)?.[1];
const electron = JSON.parse(fs.readFileSync(packagePath)).version;
const [lockRoot, lockSelf] = lockVersions(JSON.parse(fs.readFileSync(lockPath)));
if (!release.version || !/^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/.test(release.version)) {
  throw new Error("release-version.json must contain a SemVer version");
}
const values = { python, pyproject, electron, lockRoot, lockSelf };
const bad = Object.entries(values).filter(([, value]) => value !== release.version);
if (bad.length) throw new Error(`Release version ${release.version} disagrees: ${bad.map(([k, v]) => `${k}=${v}`).join(", ")}`);
console.log(`Release version OK: ${release.version}`);
