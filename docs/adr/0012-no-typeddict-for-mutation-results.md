# 0012. No TypedDict for the mutation results

- **Status:** Accepted
- **Date:** 2026-08-01

## Context

`trove/services/types.py` defines two `TypedDict`s —
`MediaItem` and `MediaPage` — for the media-grid payload that five
different service modules (`browse`, `people`, `places`, `pets`, `search`)
build and that crosses the HTTP boundary as-is. Naming that shape turns a
renamed or dropped key into a checked mypy error instead of a field quietly
going missing in the GUI.

The write endpoints under `services/` return a different kind of dict:
`{"ok": True, ...}` on success or `{"error": "..."}` on failure, with the
extra payload keys differing per call site — `{"ok": True, "name": ...}`
from a rename, `{"ok": True, "detached_faces": len(face_ids)}` from
detaching a face, `{"error": "unknown pet"}` from a lookup miss, and so on.
Counting the literal `{"ok": True ...}` / `{"error": ...}` dict constructions
inside `trove/services/*.py` today (`grep` for both patterns,
excluding `services/types.py`'s own docstring, which quotes the pattern as
prose rather than building one) finds 21 `"ok"` returns and 51 `"error"`
returns — 72 in total, spread across `archives.py`, `browse.py`,
`merging.py`, `people.py`, `pets.py`, and `places.py`.

## Decision

These stay `dict[str, Any]`. No `TypedDict` is declared for them, because
there is no one shape to name: a `total=False` `TypedDict` would still
reject any key not in its declared set, so representing 72 call sites with
genuinely different extra keys would mean writing out the union of
everything every write endpoint can return — a type that documents the
whole feature surface at once and constrains none of it usefully, since
mypy cannot check a caller against a member of a shape it never asked for.

The general rule this records: a shape earns a name in `services/types.py`
when more than one place *constructs* it — `MediaItem` and `MediaPage`
qualify because five service modules build the same fields for the same
downstream consumer. A per-mutation result dict is built in exactly one
function for exactly one endpoint; there is nothing shared to name.

## Consequences

- `services/types.py` stays limited to shapes with more than one builder.
  A new write endpoint's `{"ok": True, ...}` dict does not need a matching
  `TypedDict` added anywhere — that would be scope creep on this rule, not
  compliance with it.
- If a future refactor makes several mutations start returning the same
  extra keys (not just the shared `"ok"`/`"error"` scaffolding), that is the
  signal to name that specific shape — the trigger is a second builder
  appearing, not the total count of dicts crossing 70 or any other number.
- Callers on the frontend still only get structural guarantees from the
  `"ok"`/`"error"` keys themselves; the extra payload keys are documented in
  each service function's docstring (several already spell out the
  `{"error": ...}` case inline) rather than in a type.
