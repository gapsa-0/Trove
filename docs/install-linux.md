# Install Trove on Linux

On a supported Debian/Ubuntu release, install the `.deb` with `sudo apt install
./trove-desktop_<version>_amd64.deb`. It appears in the desktop application menu.
Alternatively, make the AppImage executable and run it: `chmod +x
Trove-<version>.AppImage && ./Trove-<version>.AppImage`.

The first time People or Pets detection runs, Trove downloads their model
weights (~550 MB) once, then works offline. Everything else — including all media
processing — is local from the start.

Linux builds do not bundle ExifTool (upstream ships Perl source rather than a
self-contained executable), so embedded EXIF is read only if `exiftool` is
installed on the system — `sudo apt install libimage-exiftool-perl`. Without it,
dates and locations still resolve from Google Takeout sidecars, filenames and
file timestamps, but camera make/model, orientation and embedded GPS are skipped.

Some distributions require FUSE for AppImage. If mounting fails, install the
distribution's FUSE compatibility package or use the `.deb`. Data lives under
`$XDG_DATA_HOME/trove` (normally `~/.local/share/trove`).

## The renderer sandbox on Ubuntu 23.10 and later

Trove's window is Chromium, which isolates the part of itself that renders your
media in a sandbox. That sandbox is built on unprivileged user namespaces, and
Ubuntu 23.10 introduced an AppArmor rule that denies those to any program without
a profile asking for one. Chromium's fallback is a small setuid helper, and its
answer to having neither is to stop before the first window.

The two downloads meet that rule differently:

- **The `.deb` installs an AppArmor profile** (`/etc/apparmor.d/trove-desktop`,
  written by its own postinstall script) that grants the permission to
  `/opt/Trove/trove-desktop`. The sandbox works normally and there is nothing to
  do. This is the reason to prefer the `.deb` on Ubuntu.
- **The AppImage cannot.** Nothing about it is installed, so there is nowhere to
  put a profile, and the image is mounted `nosuid`, which rules out the setuid
  helper as well. From 0.3.1 its launcher checks for exactly that situation and
  starts without the sandbox rather than not starting, which is what 0.3.0 did —
  it exited with a message about `chrome-sandbox` and no window. On every
  distribution that does not restrict user namespaces it changes nothing and the
  AppImage sandboxes normally. Either way it says which in *Help → Copy
  diagnostics*, and on stderr if you started it from a terminal.

Running the renderer unsandboxed is a real, if narrow, reduction: Trove loads
nothing but its own catalogue service on `127.0.0.1`, and the window cannot
navigate anywhere else, but a bug in a media decoder has one fewer wall to get
through. If you would rather keep it, install the `.deb` — or give the AppImage a
profile of its own. The one below is the `.deb`'s, pointed at the mount the image
makes for itself; the name ends in `-bin` because the file called `trove-desktop`
inside the image is the launcher script, and AppArmor attaches a profile to the
program a path names, which for a script is the shell and not the app:

```bash
sudo tee /etc/apparmor.d/trove-appimage > /dev/null <<'EOF'
abi <abi/4.0>,
include <tunables/global>

profile trove-appimage "/tmp/.mount_Trove*/trove-desktop-bin" flags=(unconfined) {
  userns,
  include if exists <local/trove-appimage>
}
EOF
sudo apparmor_parser --replace /etc/apparmor.d/trove-appimage
```

The launcher cannot see that profile from outside — it reads its own confinement,
and it is the shell, not the app — so tell it not to fall back:
`TROVE_KEEP_SANDBOX=1 ./Trove-<version>.AppImage`. If the profile is working, the
window opens as usual; if it is not, Chromium stops with the `chrome-sandbox`
message, which is the honest answer to having asked for a sandbox that is not
available.

Distributions that do not restrict unprivileged user namespaces — Fedora, Arch,
Debian itself — are unaffected either way, and the AppImage sandboxes normally
there.
