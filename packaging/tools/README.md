# Bundled native tools

`stage-tools.py` downloads the exact target-OS payloads recorded in
`manifest.json`, verifies SHA-256 before extraction, performs a version probe,
and writes the ignored `staged/<target>/` directory for PyInstaller.

The Linux x64 private-beta manifest bundles FFmpeg and FFprobe. ExifTool is
explicitly unavailable there: its upstream Linux distribution is Perl source,
not a self-contained executable, and Archive must not silently depend on the
tester's host Perl/runtime. The application continues with safe date fallbacks.
This is not sufficient for a feature-complete public release.
