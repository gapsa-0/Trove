# Security

Trove is a local-first desktop app: a Python backend serving a browser-based UI
over `http://127.0.0.1:8756` by default (`organize_archive/web/server.py`), driven
by a desktop shell in normal use. There is no cloud account and no server-side
component.

## Threat model

The backend listens on loopback with **no authentication**. Any process running
as the same user on the same machine — another app, a browser tab, a script — can
reach it and read the catalogue and the media it indexes. This is the same
exposure as any other localhost dev server; it is not a bug, but it is worth
knowing before you decide what else to run on this machine while Trove is open.

What *is* checked: POST requests (the only requests that change state) are
refused with 403 when the `Origin` header's host does not match the `Host`
header, which stops another website's tab from driving a mutation such as
removing an archive. GET requests are not checked, because they cannot change
anything. A request with no `Origin` header at all — curl, a script, the test
suite — is let through by design; that is not the case this defends against.
This is a mitigation against a confused-deputy browser tab, not general
authentication, and it does not restrict who on the machine can talk to the
server.

## Native tools and models

The app runs bundled `exiftool`, `ffmpeg` and `ffprobe`, and loads ONNX model
weights from the cache directory (`organize_archive/runtime.py`). Where each of
those comes from, and its exact byte content, is recorded in
`packaging/tools/manifest.json` and `packaging/models/manifest.json`: every
entry pins a SHA-256 digest, and `packaging/scripts/stage-tools.py` and
`packaging/scripts/stage-models.py` verify the downloaded bytes against it
before a release build is staged.

The model manifest is enforced at runtime as well, not only at build time:
`organize_archive/model_manifest.py` is what the app itself uses to resolve those
weights, and a file it downloads is accepted only if its size and SHA-256 match
the manifest — otherwise it is discarded rather than written into the cache. The
packaging script imports that module, so there is one implementation of the
check rather than two that can drift.

## Data does not leave the machine

Scanning, hashing, metadata extraction, deduplication, thumbnails, face and pet
detection, and search all run locally with no telemetry and no API keys. The
only outbound network traffic is the optional street-map tile layer (sends
coordinates only, never photos, and can be switched off) and the one-time
download of model weights the first time a feature that needs them runs. See
the Privacy section of `README.md` for the full picture.

## Reporting a vulnerability

**There is no private reporting channel.** GitHub's private vulnerability
reporting is not available on this repository, and there is deliberately no
email address here — an address nobody watches is worse than none at all. So
the honest instruction is: open a normal issue, and leave out the details that
would let someone exploit the problem before it is fixed. A description of what
is wrong and roughly where is enough to act on; a working exploit in a public
issue is not.

That is a real trade-off, not an oversight, and it is a reasonable one for what
this is: a local-first app with no server, no accounts and no user data beyond
the files already on your own disk. The realistic worst case is a bug in how
the app handles your own archive.

Expect a reply within a couple of weeks. This is a single-developer project.

## Supported versions

Trove is a single-developer project. Only the latest release
(currently `0.1.2`, see `release-version.json`) is supported; there is no
back-porting of fixes to older versions.
