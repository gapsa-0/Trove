# 0024. The AppImage starts without a sandbox it cannot have

- **Status:** Accepted
- **Date:** 2026-09-04

## Context

Trove's window is Chromium, which isolates the process that renders media in a
sandbox built on one of two mechanisms: an unprivileged user namespace, or a
setuid helper (`chrome-sandbox`, owned by root, mode 4755) for kernels that do
not hand out the first. Ubuntu 23.10 restricts unprivileged user namespaces to
programs an AppArmor profile explicitly covers, which leaves an ordinary
application with only the helper — and when neither is available, Chromium's
answer is a `FATAL` before the first window:

```
The SUID sandbox helper binary was found, but is not configured correctly.
Rather than run without sandboxing I'm aborting now.
```

0.3.0 shipped that message as the AppImage's entire behaviour on Ubuntu 23.10 and
later: `chmod +x`, run it, exit 133, no window, nothing that names Trove. The
three ways Trove ships are not equally affected, and the differences decide
everything here:

- **The `.deb` was never affected.** electron-builder generates an AppArmor
  profile granting `userns` to `/opt/Trove/trove-desktop` and a postinstall script
  that loads it into `/etc/apparmor.d/`. Verified on a restricting machine:
  `apparmor_status --enabled` succeeds, and the profile as shipped passes
  `apparmor_parser --skip-kernel-load --debug`. The same postinstall then leaves
  `chrome-sandbox` at 0755 — correctly, because with the profile loaded the
  namespace sandbox is the one in use.
- **A source checkout is fixable and already documented.** `chown root` plus
  `chmod 4755` on `node_modules/electron/dist/chrome-sandbox` is permanent, and
  `desktop/scripts/check-sandbox.cjs` prints it at install time.
- **The AppImage can do neither.** Nothing about it is installed, so there is
  nowhere to put a profile; and it is mounted `nosuid`
  (`fuse.Trove-0.3.0.AppImage ro,nosuid,nodev`), so a setuid helper could not be
  honoured even if the image carried one. It does not: inside the image
  `chrome-sandbox` is root-owned mode 0755.

Two attempts that look like the fix are not:

**Appending the switch from the main process does nothing.** `app.commandLine
.appendSwitch("no-sandbox")` at the top of `main.cjs` was measured against a
rebuilt AppImage on a restricting machine: same `FATAL`, same exit 133, and the
warning the same block writes to stderr never appeared. Chromium reads
`--no-sandbox` off the command line before it runs a line of the app's
JavaScript, so no code inside Trove can be early enough.

**Nor can the packaging be configured to do it.** electron-builder writes the
AppImage's desktop entry as `AppRun --no-sandbox %U` — its own answer to this
question is to disable the sandbox unconditionally — but only for a launch that
goes through the `.desktop` file. `AppRun` itself is embedded in the `app-builder`
binary and execs `$APPDIR/<executableName> "$@"` with nothing added, so double
clicking the image or running it from a terminal reaches Chromium bare.

## Decision

Ship a launcher inside the AppImage, and only inside the AppImage.
`desktop/scripts/appimage-launcher.sh` takes the `trove-desktop` name that
`AppRun` execs, the real binary moves to `trove-desktop-bin`, and the launcher
decides:

```
restricted    = /proc/sys/kernel/apparmor_restrict_unprivileged_userns is 1
unconfined    = /proc/self/attr/apparmor/current says "unconfined"
```

Both true, and `TROVE_KEEP_SANDBOX` unset: exec the binary with `--no-sandbox`
and say so on stderr. Otherwise exec it unchanged, which is what happens on every
distribution that does not restrict user namespaces.

`desktop/scripts/wrap-appimage-launcher.cjs` performs the rename as an
electron-builder `afterPack` hook, guarded on the AppImage being among the
targets of that invocation — so `package:linux` now runs electron-builder twice,
once per target, rather than once for both.

`desktop/src/sandbox.cjs` asks the same three questions from inside the app. It
decides nothing (it cannot), and exists to report: when it finds no sandbox
available, the About panel's diagnostics say `Renderer sandbox: off` with the
reason, because an unsandboxed renderer should not be a thing a user could only
discover by reading a launcher script.

## Consequences

**The `.deb` is the better download on Ubuntu, and the README says so.** That is
a real recommendation now rather than a preference: one download keeps the
renderer sandboxed and the other cannot.

**What is lost when the fallback fires is narrow but real.** Trove's renderer
loads exactly one origin — its own catalogue service on `127.0.0.1` — with node
integration off, context isolation on, and navigation confined to that origin. A
malicious *file* is the exposure that matters: a bug in a media decoder has one
fewer wall behind it. Set against a program that does not start at all, and
against electron-builder shipping `--no-sandbox` in the desktop entry
unconditionally, the trade is worth making — but it is a trade, which is why it
is announced twice and documented in `docs/install-linux.md` with a profile a
user can install to take it back.

**A profile written for the AppImage must name `trove-desktop-bin`.** AppArmor
attaches a profile to the program a path names, and for a `#!` script that
program is the shell. This is also why the launcher exists for the AppImage
alone: the same wrapper in the `.deb` would silently point its AppArmor profile
at bash and disable the sandbox that download currently gets right — a worse
failure than the one being fixed, because it looks like it worked.

**Packing twice costs a second copy of a ~700 MB directory per Linux build.**
The alternative is one pack with a per-target executable name, which
electron-builder does not offer. If it ever does — or if `AppRun` becomes
templatable — this collapses back to one pass, and the hook is the only thing
that needs deleting.

**The launcher cannot see a profile that would apply to the binary it execs**,
because it reads its own confinement and it is the shell. `TROVE_KEEP_SANDBOX=1`
is the escape hatch for someone who has installed one, and the honest failure
mode for getting that wrong is Chromium's original message.
