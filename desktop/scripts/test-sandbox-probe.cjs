"use strict";
// The decision src/sandbox.cjs makes, checked against the six states a machine
// can be in. None of them can be produced on the machine running this: a kernel
// either restricts unprivileged user namespaces or it does not, and the case
// that matters -- the AppImage on Ubuntu 23.10+ -- is reachable only by handing
// the pure function the state a probe would have found there.
const assert = require("assert/strict");
const { noSandboxReason, mountOptions } = require("../src/sandbox.cjs");

const suid = { setuid: true, nosuid: false };

// Sandboxed, for six different reasons.
assert.equal(noSandboxReason({ platform: "win32", restricted: false, profile: null, helper: null }), null);
assert.equal(noSandboxReason({ platform: "darwin", restricted: false, profile: null, helper: null }), null);
// Fedora, Arch, stock Debian: namespaces are there for the asking.
assert.equal(noSandboxReason({ platform: "linux", restricted: false, profile: null, helper: null }), null);
// The .deb on Ubuntu 24.04: its postinst loaded a profile that grants userns.
assert.equal(noSandboxReason({ platform: "linux", restricted: true, profile: "trove-desktop (unconfined)", helper: suid }), null);
// A source checkout whose helper was given to root, as check-sandbox.cjs asks.
assert.equal(noSandboxReason({ platform: "linux", restricted: true, profile: null, helper: suid }), null);

// Not sandboxed: the AppImage, root-owned 0755 on a nosuid mount. Both halves
// are true of it, and either alone is enough.
const appImage = noSandboxReason({ platform: "linux", restricted: true, profile: null, helper: { setuid: false, nosuid: true } });
assert.match(appImage, /restricts unprivileged user namespaces/);
assert.match(noSandboxReason({ platform: "linux", restricted: true, profile: null, helper: { setuid: true, nosuid: true } }), /nosuid/);
assert.match(noSandboxReason({ platform: "linux", restricted: true, profile: null, helper: { setuid: false, nosuid: false } }), /not setuid root/);
// A checkout that npm unpacked, or a layout missing the helper entirely.
assert.match(noSandboxReason({ platform: "linux", restricted: true, profile: null, helper: null }), /no chrome-sandbox helper/);

// The mount carrying a path is the longest mount point that is a prefix of it,
// not the first one that matches: / is a prefix of everything.
const mountinfo = [
  "25 0 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw",
  "211 33 0:88 / /tmp/.mount_Trove-iNlF1M ro,nosuid,nodev,relatime shared:298 - fuse.Trove Trove ro,user_id=1000",
  "212 33 0:89 / /home/two\\040words ro,nosuid - fuse.x x ro",
].join("\n");
assert.deepEqual(mountOptions("/opt/Trove/chrome-sandbox", mountinfo), ["rw", "relatime"]);
assert.ok(mountOptions("/tmp/.mount_Trove-iNlF1M/chrome-sandbox", mountinfo).includes("nosuid"));
// A mount point is a directory: /tmp/.mount_Trove-iNlF1MX is not inside it.
assert.deepEqual(mountOptions("/tmp/.mount_Trove-iNlF1MX/chrome-sandbox", mountinfo), ["rw", "relatime"]);
// mountinfo octal-escapes the space, so an AppImage under it still matches.
assert.ok(mountOptions("/home/two words/chrome-sandbox", mountinfo).includes("nosuid"));

console.log("sandbox probe OK");
