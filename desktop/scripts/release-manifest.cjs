"use strict";
const crypto = require("crypto"), fs = require("fs"), path = require("path");
const root = path.resolve(__dirname, "../..");
const dir = path.resolve(process.argv[2] || path.join(root, "desktop", "release"));
const version = JSON.parse(fs.readFileSync(path.join(root, "release-version.json"))).version;
// Only the installers users download get a checksum. electron-builder also drops
// its own debug log next to them, and re-running this script would otherwise
// checksum its previous output.
const RELEASE_ARTIFACT = /\.(AppImage|deb|exe|msi|zip|blockmap)$/i;
const files = fs.readdirSync(dir).filter(name =>
  fs.statSync(path.join(dir, name)).isFile() && RELEASE_ARTIFACT.test(name)).map(name => {
  const file = path.join(dir, name); return { name, size: fs.statSync(file).size, sha256: crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex") };
});
fs.writeFileSync(path.join(dir, "release-manifest.json"), JSON.stringify({ version, commit: process.env.ARCHIVE_BUILD_COMMIT || "unknown", artifacts: files }, null, 2));
fs.writeFileSync(path.join(dir, "SHA256SUMS.txt"), files.map(f => `${f.sha256}  ${f.name}`).join("\n") + "\n");
