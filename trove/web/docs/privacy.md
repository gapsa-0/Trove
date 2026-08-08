---
title: Privacy and data
summary: Every network call the app can make, and everything it stores about you.
---

Every model on these pages runs on this computer. There is no account, no API
key, and no setting anywhere in the app that would send archive content to a
service. No photo, thumbnail, filename, search query, or catalogue record
leaves the machine.

That is a stronger claim than "we don't sell your data", so this page states
exactly what the exceptions are.

## Everything the app can send

There are two, and neither of them carries content.

**Downloading model weights.** A feature you enable fetches its weights once,
from GitHub or Hugging Face, into a shared cache. That is a request for a file,
identical for every user, and it happens once per machine rather than once per
archive. A feature you did not choose downloads nothing at all, because there
is no stage to fetch anything: an archive set up for indexing and duplicates
only never touches the network.

| Feature | Downloads | When |
| --- | --- | --- |
| [Indexing](indexing.md) | Nothing | Never |
| [Duplicates](duplicates.md) | Nothing | Never |
| [Places](places.md) | Nothing | Never |
| [People](people.md) | 275 MB | The archive's first detection pass |
| [Pets](pets.md) | 35 MB | The archive's first detection pass |
| [Search by description](search.md) | 715 MB | First indexing pass, then the text half on first search |
| [Search by document text](documents.md) | Nothing | Never |
| [Search by picture text](ocr.md) | 30 MB | The archive's first text pass |

No model is bundled with the installer: every one of them is fetched once, on
the first run of the feature that needs it, and checked against a known
fingerprint before it is used. A new installation therefore needs a connection
once. Once the weights are cached, everything works fully offline, and a second
archive that wants the same feature downloads nothing.

**The map's street-map layer.** Turning it on fetches public map tiles, which
discloses to that tile server the coordinates you are currently looking at,
never the photos. It is a toggle, and switching it off leaves a fully offline
plot. It is the only outbound call in the app that depends on your data at all.

There is no telemetry, no crash reporting, and no update check.

## What is stored, and where

Trove never writes to your media folder. It does not move, rename, edit, or
delete originals, including duplicates, which are hidden from browsing and
left on disk.

Everything it derives lives in its own data directory, one isolated database
and cache per archive:

- **The catalogue**, a SQLite database: one row per file, with its path, size,
  hashes, resolved date, and coordinates.
- **Thumbnails**, cached so the grid does not decode originals repeatedly.
- **Face and animal crops**, cached for the People and Pets screens.
- **Vectors**: face embeddings, pet embeddings, and search embeddings. These
  are numbers describing appearance, not images, and they never leave the
  machine either.
- **`config.json`**, holding your settings and each archive's chosen features.
  Every threshold named on these pages can be overridden here.

Deleting an archive from Trove removes its database and cache. Your media
folder is untouched, because Trove never had anything of yours in there.

## Why the models are the local kind

Running a 689 MB model on a laptop CPU is slower than calling an API, and it
was a deliberate trade. Search by description used to call a cloud service, and
it was the one place the app sent photos and typed queries off the machine.
That path was removed rather than left switched off.

Everything now runs through onnxruntime on your CPU: the face detector and
embedder, the animal detector and re-identification model, both halves of the
search model, and the query translator. The reasoning, and what it cost, is
recorded in full as `docs/adr/0003-local-only-ml.md` in the repository.
