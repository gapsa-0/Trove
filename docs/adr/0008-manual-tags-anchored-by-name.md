# 0008. Manual person/pet tags are anchored by name, not by id

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

Clustering into `persons` and pet identities is a full re-derivation, not an
incremental update: every re-cluster rebuilds those tables wholesale from the
current set of faces/animal detections. That means a `persons.id` from
before a re-cluster has no guaranteed relationship to any id after it — the
same person's cluster can end up under a different id, or briefly not exist
at all mid-rebuild. A manual correction — "this photo is of Mari, even
though no face was detected" — has to survive that rebuild, so it cannot
reference an id that the rebuild is free to discard.

## Decision

Manual tags reference people and pets by **name**, not by id, and a repair
step re-points them after every clustering run.

`trove/faces/manual_tags.py`'s module docstring states the
reasoning directly: "These tags are anchored by person NAME rather than id,
because clustering rebuilds `persons` wholesale on every pass — so they need
re-pointing after each pass." Its one function, `repair_manual_person_files`,
walks every `person_files` row, and for each one whose stored `person_id` no
longer carries the row's stored `person_name`, looks up whichever person
*currently* carries that name and re-points the row at it (dropping a losing
duplicate if the target `(person_id, file_id)` pair already exists, since
that pair is the table's primary key). If no person currently carries the
name at all, the row is left untouched rather than deleted — the docstring
notes the name "may come back on a later pass," and deleting a user's
statement just because a clustering pass momentarily lost the name would be
data loss.

This repair is wired into the clustering transaction itself, not run as a
separate step someone could forget: `trove/faces/cluster.py`
calls `repair_manual_person_files(conn)` inside its finalize step, on the
same open connection and before the caller commits. The pet side is the
identical shape: `trove/pets/manual_tags.py`'s
`repair_manual_pet_files`, called from `trove/pets/cluster.py` at
every return path that commits a re-cluster (the module comment there notes
"every return path below must repair before it commits").

**Consequence, stated explicitly by the code that enforces it: only named
people and pets can carry manual tags.**
`trove/services/people_edit.py`'s
`add_person_to_file` — "Tag a file with a named person by hand, for media
where no face was detected at all" — checks `if not p or not p["name"]:
return {"error": "target must be a named person"}`, and its docstring gives
the reason: "an unnamed auto-cluster id is ephemeral and wouldn't survive the
next re-cluster anyway." `trove/services/pets_edit.py`'s
`add_pet_to_file` is, per its own docstring, "Same shape as
add_person_to_file" and enforces the identical rule ("target must be a named
pet").

**Durable must-link / cannot-link constraints exist for the same underlying
reason.** `face_links` (faces/cluster.py) and `pet_links` (pets/cluster.py)
record durable "same person?"/"different person?" answers from review — a
merge or a "this is not this person" correction. These, too, have to survive
a rebuild that discards and reassigns ids: `face_links` is anchored to *face*
ids, which are stable across a re-cluster (only the `persons` grouping above
them is rebuilt), and `trove/faces/migrate_adaface.py` — which
handles the harder case of a full re-extract that also changes face ids —
explicitly snapshots and remaps `face_links` and `pet_links` through that
migration rather than letting them dangle.

## Consequences

- A person or pet must be named before it can accept a manual tag; an
  unreviewed auto-cluster cannot, because there is nothing stable to anchor
  the tag to.
- Renaming a person changes what a manual tag anchors to implicitly — the
  next re-cluster's repair step follows the name, not a stored id, so a
  rename effectively carries any manual tags along with it (as long as the
  new name is what the repaired rows expect).
- If a name is renamed *and* the archive is re-clustered before any repair
  runs, a manual tag can temporarily point at a person id that no longer
  carries that name; the row is left alone rather than dropped, and picks
  the correct target back up whenever a person with that name exists again.
