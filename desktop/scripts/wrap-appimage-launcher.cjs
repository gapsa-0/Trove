"use strict";

// electron-builder's `afterPack` hook: put scripts/appimage-launcher.sh in front
// of the Electron binary, for AppImage builds only.
//
// Only the AppImage. The .deb ships its own AppArmor profile and needs no
// launcher, and giving it one would *break* that profile: AppArmor attaches a
// profile to the binary a path names, and for a #! script that binary is the
// interpreter -- so a profile written for /opt/Trove/trove-desktop would apply
// to bash and never to Chromium, which is a worse failure than the one this
// fixes because it looks like it worked.
//
// electron-builder packs once per invocation and both Linux targets read the
// same directory, so "only the AppImage" means packing twice: see
// `package:linux` in package.json. Each pack empties the output directory first
// (ElectronFramework.unpack), so the .deb's pass never sees this rename.

const fs = require("fs");
const path = require("path");

exports.default = async function wrapAppImageLauncher(context) {
  if (!context.targets.some(target => target.name === "appImage")) return;

  const executable = context.packager.executableName;
  const launcher = path.join(context.appOutDir, executable);
  const binary = path.join(context.appOutDir, `${executable}-bin`);

  // AppRun execs $APPDIR/<executableName>, so the launcher has to take that
  // name and the real binary has to move aside. Chromium re-execs itself for its
  // child processes through /proc/self/exe, which resolves to the binary rather
  // than to this script, so the zygote is unaffected by the indirection.
  fs.renameSync(launcher, binary);
  fs.copyFileSync(path.join(__dirname, "appimage-launcher.sh"), launcher);
  fs.chmodSync(launcher, 0o755);
};
