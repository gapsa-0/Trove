# Install Archive on Linux

On a supported Debian/Ubuntu release, install the `.deb` with `sudo apt install
./Archive*.deb`. It appears in the desktop application menu. Alternatively, make
the AppImage executable and run it: `chmod +x Archive*.AppImage && ./Archive*.AppImage`.

Some distributions require FUSE for AppImage. If mounting fails, install the
distribution's FUSE compatibility package or use the `.deb`. Data lives under
`$XDG_DATA_HOME/organize_archive` (normally `~/.local/share/organize_archive`).
