"""The parts of a "same X?" merge that more than one entity type shares.

People, pets and places all let a user say "these two are the same thing",
and the three implementations grew up copying each other. They are not
equally alike, though, so this module is shaped around where they actually
agree rather than around the fact that all three are spelled "merge":

- **All three refuse a merge identically.** Same checks, same order, same
  error strings -- and those strings reach the user verbatim, so they are
  part of the contract rather than an implementation detail. ``load_sides``
  and ``resolve_name`` are that preamble. ``place_merge_preview`` uses them
  too, which is what stops the confirmation dialog from accepting a pair the
  merge itself would reject.

- **People and pets share the whole transaction.** Both re-point child rows
  onto the survivor, delete the loser, and write a durable link anchored to
  a *child* id rather than an entity id, because the clusterer rebuilds
  `persons`/`pets` wholesale and an entity id would not survive that.

- **Places do not, and are deliberately left out of the second half.** A
  place is durable -- nothing rebuilds `place_clusters` underneath a merge
  -- so its undo is a true restore of the dropped row rather than a
  retracted constraint plus a re-cluster. `schema.sql` says the same thing
  in `place_merges`' comment. Running it through the linked path would mean
  a generic function carrying a place-shaped branch, which is worse than the
  one honest copy that lives in `places.py`.

What is emphatically *not* shared is the survivor rule. All three differ,
and two of them differ in ways nothing had ever tested (see
``tests/integration/test_merge_characterisation.py``): people break a tie on
argument order, pets on the lower id. Each module passes in its own already
chosen survivor, so unifying those rules stays a deliberate decision for
someone to make on purpose rather than something this module quietly
imposes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EntitySpec:
    """What the shared preamble needs to know about one entity type.

    ``singular``/``plural`` are the nouns that appear in the error strings, so
    they are as load-bearing as the table name: "unknown person" and "need two
    distinct persons" are matched literally by the characterisation tests and
    shown to the user as-is.
    """

    singular: str
    plural: str
    table: str
    columns: str


def load_sides(conn, spec: EntitySpec, id_a, id_b):
    """Validate a merge's two ids and load both rows.

    Returns ``(row_a, row_b, None)``, or ``(None, None, error_dict)`` if the
    pair cannot be merged. The two refusals here are the ones every entity
    type shares: an id that is missing or repeated, and an id that names
    nothing.
    """
    if not id_a or not id_b or id_a == id_b:
        return None, None, {"error": f"need two distinct {spec.plural}"}
    pa = conn.execute(f"SELECT {spec.columns} FROM {spec.table} WHERE id=?", (id_a,)).fetchone()
    pb = conn.execute(f"SELECT {spec.columns} FROM {spec.table} WHERE id=?", (id_b,)).fetchone()
    if not pa or not pb:
        return None, None, {"error": f"unknown {spec.singular}"}
    return pa, pb, None


def resolve_name(pa, pb, name):
    """Normalise an explicit merge name and refuse an unresolvable clash.

    Returns ``(name, None)`` or ``(None, error_dict)``. Two differently-named
    entities cannot be merged without being told which name to keep: there is
    no automatic way to choose between two names a user typed, and silently
    dropping one of them is the kind of loss that is only noticed much later.
    Every other combination resolves on its own.
    """
    name = (name or "").strip() or None
    if pa["name"] and pb["name"] and pa["name"] != pb["name"] and not name:
        return None, {"error": f"both named ({pa['name']} / {pb['name']}); choose a name"}
    return name, None
