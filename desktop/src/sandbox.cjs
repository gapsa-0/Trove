"use strict";

/* Whether Chromium can sandbox its renderer on this machine -- asked before it
   tries, because its own answer to "no" is to abort.

   Chromium confines the renderer either with an unprivileged user namespace or,
   where it cannot have one, with a setuid helper: `chrome-sandbox`, owned by
   root with the setuid bit. Ubuntu 23.10 and later deny the first to any program
   not covered by an AppArmor profile that grants `userns`, so both mechanisms
   can be missing at once -- and then Electron prints a FATAL and exits before
   the first window ever appears.

   Trove ships three ways, and they meet this very differently:

   * The .deb installs an AppArmor profile granting `userns` (electron-builder
     writes it, the generated postinst loads it as /etc/apparmor.d/trove-desktop),
     so the namespace sandbox works and this file finds nothing to do.
   * A source checkout is a real tree on a real filesystem: `chown root` plus
     `chmod 4755` on the helper fixes it permanently, which is what
     scripts/check-sandbox.cjs prints at install time.
   * The AppImage can do neither. It is a single file, nothing about it is
     installed, so there is nowhere to put a profile -- and it is mounted
     `nosuid`, so a setuid helper could not be honoured even if the image
     carried one at 4755. It does not: inside the image chrome-sandbox is
     root-owned mode 0755.

   So for the AppImage the choice is between starting with the renderer sandbox
   off and not starting at all, and only the first of those is a program. This
   module reports which case it is; main.cjs decides, and records the reason in
   the diagnostics so a disabled sandbox is never silent. */

const fs = require("fs");
const path = require("path");

// 1 means unprivileged user namespaces are restricted to AppArmor profiles that
// ask for them. Absent on every distribution that does not do this at all.
const RESTRICTION = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns";
// The newer path is the one to prefer; kernels before 5.1 only have the second.
const CONFINEMENT = ["/proc/self/attr/apparmor/current", "/proc/self/attr/current"];
const SETUID = 0o4000;

function readText(file) {
  try {
    return fs.readFileSync(file, "utf8").trim();
  } catch {
    return null;
  }
}

/* The options of the mount that carries `target`: the longest mount point that
   is a prefix of it. mountinfo escapes spaces and tabs in paths as octal, which
   matters here because an AppImage run from a directory with a space in its name
   is otherwise matched against the wrong mount. */
function mountOptions(target, mountinfo = readText("/proc/self/mountinfo")) {
  let best = "";
  let options = "";
  for (const line of (mountinfo || "").split("\n")) {
    const fields = line.split(" ");
    if (fields.length < 6) continue;
    const point = fields[4].replace(/\\(\d{3})/g, (_, code) => String.fromCharCode(parseInt(code, 8)));
    const inside = target === point || target.startsWith(point.endsWith("/") ? point : `${point}/`);
    if (inside && point.length >= best.length) {
      best = point;
      options = fields[5];
    }
  }
  return options.split(",");
}

/* What the three questions answer on this machine. Split from the decision below
   so the decision can be tested without a kernel that answers them a particular
   way -- there is no other way to exercise the Ubuntu case from a machine that
   is not Ubuntu, or the working case from one that is. */
function probeSandbox(helper, platform = process.platform) {
  const state = { platform, restricted: readText(RESTRICTION) === "1", profile: null, helper: null };
  if (platform !== "linux") return state;
  const confinement = CONFINEMENT.map(readText).find(value => value !== null);
  state.profile = confinement && confinement !== "unconfined" ? confinement : null;
  try {
    const stat = fs.statSync(helper);
    state.helper = {
      setuid: stat.uid === 0 && (stat.mode & SETUID) !== 0,
      nosuid: mountOptions(path.resolve(helper)).includes("nosuid"),
    };
  } catch {
    // No helper where one was expected. Chromium would say the same thing in its
    // own words; either way the setuid route is not open.
    state.helper = null;
  }
  return state;
}

/* null when the renderer can be sandboxed, otherwise why it cannot be.

   The `profile` case deliberately trusts any profile at all rather than looking
   for `userns` in it: the one profile Trove ships is the one the .deb installs,
   which exists precisely to grant it, and a profile applied by someone else is
   theirs to answer for. Getting that wrong costs an abort at startup -- exactly
   what happens today -- and never a sandbox quietly switched off. */
function noSandboxReason(state) {
  if (state.platform !== "linux") return null;
  if (!state.restricted) return null;
  if (state.profile) return null;
  if (state.helper && state.helper.setuid && !state.helper.nosuid) return null;
  const detail = !state.helper
    ? "no chrome-sandbox helper beside the executable"
    : state.helper.nosuid
      ? "its chrome-sandbox helper is on a nosuid mount"
      : "its chrome-sandbox helper is not setuid root";
  return `this system restricts unprivileged user namespaces, no AppArmor profile covers Trove, and ${detail}`;
}

module.exports = { probeSandbox, noSandboxReason, mountOptions };
