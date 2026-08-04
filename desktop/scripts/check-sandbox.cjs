"use strict";

// Say, before Electron does, what it otherwise only says by aborting.
//
// Chromium isolates its renderer in a sandbox built either on unprivileged user
// namespaces or on a setuid helper binary. Ubuntu 23.10 and later restrict the
// first for ordinary programs, so Electron falls back to the helper --
// `chrome-sandbox`, which works only when owned by root with the setuid bit. npm
// unpacks it as the installing user, so from a source checkout it never is, and
// `npm run dev` aborts with a FATAL that names the file and mode it wants.
//
// That message is clear, but it arrives after a successful install, on a
// different command, possibly days later. So this runs at each of the three
// points where it lands: `postinstall`, while the install that caused it is
// still on screen; the end of `make setup`, where npm's own summary would
// otherwise scroll it away; and `predev`, right above the FATAL itself, for the
// install that happened ten minutes and one `cd` ago. It only reports; changing
// the ownership needs root, which an install script has no business asking for.
//
// Deliberately never fails: a hint that can break `npm ci` is worse than the
// papercut it describes. That also leaves `npm run dev -- --no-sandbox` working,
// which is just as well, because npm passes those arguments to `dev` alone --
// from `predev` there is no way to see that the note is already moot.

const fs = require("fs");
const path = require("path");

const SANDBOX = path.join(__dirname, "..", "node_modules", "electron", "dist", "chrome-sandbox");
const RESTRICTION = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns";
const SETUID = 0o4000;

// Only distributions that restrict unprivileged user namespaces need the setuid
// helper at all; on Fedora, Arch and stock Debian, Chromium sandboxes itself
// with namespaces and this whole file has nothing to say.
function restrictsUserNamespaces() {
  try {
    return fs.readFileSync(RESTRICTION, "utf8").trim() === "1";
  } catch {
    return false;
  }
}

function needsOwnership() {
  try {
    const stat = fs.statSync(SANDBOX);
    return stat.uid !== 0 || !(stat.mode & SETUID);
  } catch {
    // No binary to report on: either a platform without one, or an install that
    // did not unpack -- which `make setup` and CI both check for by name.
    return false;
  }
}

if (process.platform === "linux" && restrictsUserNamespaces() && needsOwnership()) {
  const relative = path.relative(path.join(__dirname, ".."), SANDBOX);
  console.log(
    [
      "",
      "Note: this system restricts unprivileged user namespaces, so Electron needs",
      "its setuid sandbox helper to be owned by root. Until it is, `npm run dev`",
      "aborts at launch. From desktop/:",
      "",
      `  sudo chown root:root ${relative}`,
      `  sudo chmod 4755 ${relative}`,
      "",
      "Redo this after any npm install/ci, which replaces the file.",
      "To skip the sandbox instead:  npm run dev -- --no-sandbox",
      "Packaged builds set this themselves; only source checkouts need it.",
      "",
    ].join("\n"),
  );
}
