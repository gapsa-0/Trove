# Install Archive on Linux

On a supported Debian/Ubuntu release, install the `.deb` with `sudo apt install
./Archive*.deb`. It appears in the desktop application menu. Alternatively, make
the AppImage executable and run it: `chmod +x Archive*.AppImage && ./Archive*.AppImage`.

The first time People or Pets detection runs, Archive downloads their model
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
