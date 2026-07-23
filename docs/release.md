# Release process

Versions use SemVer and are declared once in `release-version.json`; public tags
are `v<version>`. The CI version gate requires Python, Electron, and the canonical
value to agree. After intentionally changing the canonical value, run
`npm run sync:version` from `desktop/`, review its three generated updates, and
commit them together. Candidate builds are native CI artifacts, never developer uploads.

## Required decisions before public beta

- Publisher identity: **not yet recorded**.
- Windows signing authority: **not yet configured**.
- Release host and supported Ubuntu/Debian versions: **not yet recorded**.
- Public-beta audience and feedback channel: **not yet recorded**.

The public Windows workflow fails closed when signing credentials are unavailable.
Record the actual signing identity and supported platforms here before enabling the
protected `public-release` environment. To roll back, withdraw the affected release,
publish the prior known-good artifacts and checksums, and notify beta users. Send
security reports through the project's private maintainer contact once established.

## Clean-machine acceptance

Before publishing, record a run on a clean Windows x64 account and the selected
Ubuntu/Debian x64 release: install, native folder selection, small fixture indexing,
restart persistence, upgrade, and uninstall. Verify the Windows installer and installed
executable signatures; verify Linux executable permissions and AppImage/FUSE behaviour.
Confirm source media is untouched and explicitly record whether app data is retained.
