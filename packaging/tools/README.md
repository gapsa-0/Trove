# Bundled native tools

`stage-tools.py` downloads the exact target-OS payloads recorded in
`manifest.json`, verifies SHA-256 before extraction, performs a version probe,
and writes the ignored `staged/<target>/` directory for PyInstaller.

Both `linux-x64` and `win32-x64` bundle FFmpeg and FFprobe from the same BtbN
build, so the two platforms decode identically.

ExifTool differs by platform. The Windows payload is upstream's self-contained
build: the archive member `exiftool(-k).exe` is staged as `exiftool.exe`
(`install_as`) together with its required `exiftool_files/` Perl runtime
(`support_dir`), because `organize_archive.runtime.tool` looks up a bare
`exiftool.exe` and the executable will not start without that directory beside
it. It is pinned from SourceForge rather than exiftool.org, which serves only the
current version and would break the pin at the next upstream release.

ExifTool is explicitly `unavailable` on Linux: its upstream Linux distribution is
Perl source, not a self-contained executable, and Archive must not silently
depend on the tester's host Perl/runtime. The application continues with safe
date fallbacks (Takeout sidecars, filename parsing, mtime), but Linux builds read
no embedded EXIF — camera make/model, orientation and GPS are missing for files
with no Takeout sidecar. This is a known gap for the Linux public release.

A payload can be staged from a different host OS (useful for checking a download,
its hash and its layout); the version probe is then skipped and recorded as such
in `tools-build-info.json`. Release CI always stages each target natively, so its
artifacts carry a real probe.
