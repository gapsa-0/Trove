# 0015. An archive chooses its features, and the pipeline is the only gate

- **Status:** Accepted
- **Date:** 2026-08-04

## Context

Every archive ran every stage this app knows how to run. That is the wrong
default for two reasons, and the second one is expensive.

A folder of scanned paperwork has no faces worth clustering and a phone dump of
untagged photos has no coordinates to map, so some archives were spending hours
of CPU producing pages nobody would open. And the model weights are not small:
275 MB for the face detector, 689 MB for the two SigLIP towers, on top of the
~350 MB of self-exported models the installers bundle. Someone who wanted
duplicate photos found was made to download all of it, and there was nowhere to
say otherwise.

There was already a per-stage *pause* (ADR 0005, `paused_stages` on the archive's
registry entry), and the obvious move was to reuse it: pause the stages you do
not want. It is the wrong mechanism. A paused stage is still the archive's work,
temporarily stopped — it keeps its card, it reports itself stalled, it appears in
the "waiting on" chains of everything behind it, and the moment it is resumed it
downloads the weights it always would have. Pause answers "not now". The question
here is "not ever, unless I say so", and the two want opposite things from every
screen that renders them.

The other candidate was a `Config` flag per feature, app-wide. That fails the
first thing anyone does with this app: two archives, one of family photos where
People matters and one of documents where it does not.

## Decision

An archive's feature set is part of the archive, stored on its registry entry in
`config.json` beside `paused` / `paused_stages`, chosen when the archive is
created and changeable afterwards through the same screen.

`organize_archive/features.py` is the catalogue: one table of id, label, the
prose the setup panel shows, which stage kinds the feature owns, which nav
sections it unlocks, and what its weights cost. It is L0 and names stage kinds
and detector names as plain strings; `tests/unit/test_features.py` checks those
spellings against the pipeline rather than letting an import invert the layering.

**There is exactly one gate: `stage_states` leaves a disabled stage out of the
list.** Everything else follows from that and needs no code of its own — the
scheduler starts whatever the list calls queued, so a stage that is not in the
list is never started; a stage is what downloads its own weights, so weights are
never fetched; `cards()` builds a card per member stage, so the Overview loses
the card. Two features owning one stage (People and Pets share the fused detect
pass, ADR 0004) resolve as "runs if either is on", with the fused pass told which
detectors it was asked for.

Switching a feature off deletes nothing. The stage stops being scheduled and the
section disappears; the faces, embeddings and places already found stay in the
catalogue, so switching it back on resumes rather than restarts.

Indexing (scan + enrich) and Duplicates (dedup) cannot be switched off. Every
other stage depends on them, and `tests/unit/test_features.py` enforces that no
stage may depend on an optional feature — without that, a stage whose dependency
was switched off would wait for a state that can never arrive.

## Consequences

- The archive setup screen (`web/static/js/setup.js`) is the only place features
  are chosen, for a new archive and for an existing one. There is no second,
  thinner settings pane to drift from it.
- A new archive starts with the required features only. Everything else is
  opt-in, which is the point: the screen exists so that nobody downloads 689 MB
  without having been asked.
- **The detect pass had to learn which half it is running.** A one-sided pass
  must not write the other detector's scan marker (the backlog would never
  settle for a re-enabled feature) and must not delete the other detector's rows
  (switching Pets off would destroy every animal already found). Both are in
  `detect/persist.py`, keyed on the `want` set, and covered by
  `tests/integration/test_detect_one_sided.py`.
- People and Pets are separately choosable but still one decode pass. ADR 0004
  stands; a test asserts the detect card never resolves to two stages.
- An archive registered before this existed has no `features` key, and
  `features.resolve(None)` answers with the full set. An upgrade cannot switch
  off work an archive has been doing for months.
- The feature catalogue is now the place a new capability announces itself. The
  document-text and OCR work planned next arrives as two rows in that table plus
  a stage each, rather than as work every archive silently starts paying for.
