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

What is *not* shared is the survivor rule. Each entity ranks on what it has --
a named side first, then face_count, detection_count, or pinned-then-member
count -- and each passes in its own already chosen survivor. They do now agree
on the last step: a tie falls to the lower id, so no merge depends on which
side the caller happened to pass first (people used to, and a drag-merge's
outcome changed with the direction of the drag). Keeping the rules here would
mean a generic function carrying three entities' notions of "bigger"; keeping
them apart means a new entity states its own, and states the id tiebreak with
it. See ``tests/integration/test_merge_characterisation.py``, which pins all
three in both argument orders.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..db import database as db


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


def load_sides(
    conn: sqlite3.Connection, spec: EntitySpec, id_a: int | None, id_b: int | None
) -> tuple[sqlite3.Row, sqlite3.Row, None] | tuple[None, None, dict[str, str]]:
    """Validate a merge's two ids and load both rows.

    Returns ``(row_a, row_b, None)``, or ``(None, None, error_dict)`` if the
    pair cannot be merged. The two refusals here are the ones every entity
    type shares: an id that is missing or repeated, and an id that names
    nothing.

    **For merges only.** Other operations take two ids and validate them the
    same way -- ``people._persons_link`` records a cannot-link between two
    clusters -- and deliberately do not call this, because both of their sides
    survive. Reusing this helper there would save four lines and cost the
    distinction: a reader who sees ``load_sides`` should be able to conclude
    that two entities are about to become one. Widening it to mean "validate
    two ids" is how that stops being true.
    """
    if not id_a or not id_b or id_a == id_b:
        return None, None, {"error": f"need two distinct {spec.plural}"}
    pa = conn.execute(f"SELECT {spec.columns} FROM {spec.table} WHERE id=?", (id_a,)).fetchone()
    pb = conn.execute(f"SELECT {spec.columns} FROM {spec.table} WHERE id=?", (id_b,)).fetchone()
    if not pa or not pb:
        return None, None, {"error": f"unknown {spec.singular}"}
    return pa, pb, None


def resolve_name(
    pa: sqlite3.Row, pb: sqlite3.Row, name: str | None
) -> tuple[str | None, None] | tuple[None, dict[str, str]]:
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


@dataclass(frozen=True)
class LinkedSpec:
    """An entity whose merge is made durable by a link between two *children*.

    People and pets both have this shape. The clusterer rebuilds `persons` and
    `pets` wholesale, so a merge recorded against an entity id would be undone
    by the next pass; recorded against a face or a detection id, which the
    rebuild does not touch, it survives. That is the whole reason the link
    tables exist, and it is why every column below names a child rather than an
    entity.
    """

    entity: EntitySpec
    child_table: str
    fk: str
    merge_table: str
    child_ids_column: str
    link_table: str
    link_a: str
    link_b: str


def record_link(
    conn: sqlite3.Connection,
    spec: LinkedSpec,
    child_a: int | None,
    child_b: int | None,
    kind: str,
    now: str,
) -> tuple[int, int] | None:
    """Write a durable link constraint between two child rows.

    Returns the ``(sorted)`` pair it wrote, or ``None`` if there was nothing to
    link -- callers that need to record exactly which link a merge produced
    (``*_merges.link_a``/``link_b``, for undo) key off this return value rather
    than re-deriving the sort.
    """
    if not child_a or not child_b or child_a == child_b:
        return None
    a, b = sorted((child_a, child_b))
    conn.execute(
        f"INSERT OR REPLACE INTO {spec.link_table}"
        f"({spec.link_a}, {spec.link_b}, kind, created_at) VALUES(?,?,?,?)",
        (a, b, kind, now),
    )
    return (a, b)


def merge_linked(
    conn: sqlite3.Connection,
    spec: LinkedSpec,
    keep: sqlite3.Row,
    drop: sqlite3.Row,
    *,
    survivor_name: str | None,
    rep: Callable[[sqlite3.Connection, sqlite3.Row], int | None],
    finish: Callable[[sqlite3.Connection, sqlite3.Row, str | None], None],
    now: str,
) -> int:
    """Move the loser's children onto the survivor and record how to undo it.

    ``keep``/``drop`` are already chosen by the caller: survivor rules differ
    per entity and stay where a reader of that entity can see them.
    ``rep(conn, side)`` yields the child id that anchors that side's link.
    ``finish(conn, keep, survivor_name)`` applies whatever bookkeeping the
    entity does to a survivor afterwards -- recounting, a centroid, a name.

    **Does not commit**, unlike ``unmerge_linked``, and the asymmetry is
    deliberate: both callers read the survivor row back afterwards to build
    their response, so the commit has to happen in the caller, between this
    and that read. Undo has nothing to read back and so owns its own commit.
    """
    # Captured before the UPDATE below moves them: this is exactly the set the
    # undo needs in order to know which children this merge folded in.
    drop_children = [
        r[0]
        for r in conn.execute(
            f"SELECT id FROM {spec.child_table} WHERE {spec.fk}=?", (drop["id"],)
        ).fetchall()
    ]
    link = record_link(conn, spec, rep(conn, keep), rep(conn, drop), "same", now)
    # foreign_keys=ON (see db.connect): the child's foreign key is
    # ON DELETE SET NULL, so the UPDATE moving drop's children MUST run before
    # the DELETE, or they would be orphaned instead of merged.
    conn.execute(
        f"UPDATE {spec.child_table} SET {spec.fk}=? WHERE {spec.fk}=?",
        (keep["id"], drop["id"]),
    )
    conn.execute(f"DELETE FROM {spec.entity.table} WHERE id=?", (drop["id"],))
    finish(conn, keep, survivor_name)
    # Recorded so this merge can be undone later: which children moved, what
    # the losing side was called, and which link row to retract. The row's id
    # goes back to the caller, which is what lets an edit_log entry point at
    # the record that owns the reversal instead of copying it.
    cur = conn.execute(
        f"""INSERT INTO {spec.merge_table}(survivor_id, survivor_name, dropped_name,
                        {spec.child_ids_column}, link_a, link_b, created_at)
            VALUES(?,?,?,?,?,?,?)""",
        (
            keep["id"],
            survivor_name,
            drop["name"],
            json.dumps(drop_children),
            link[0] if link else None,
            link[1] if link else None,
            now,
        ),
    )
    return int(cur.lastrowid or 0)


def unmerge_linked(
    conn: sqlite3.Connection,
    spec: LinkedSpec,
    merge_id: int | None,
    *,
    restore: Callable[[sqlite3.Connection, sqlite3.Row], None] | None = None,
) -> dict[str, Any]:
    """Undo a merge recorded by ``merge_linked``.

    ``restore(conn, merge_row)`` is the entity's own extra restoration, if it
    has one; people re-pin face names, pets have nothing to put back.

    Safe to call twice: a stale or missing ``merge_id`` returns a clean error
    rather than raising. Returns ``recluster`` so the caller knows to re-run
    the clusterer -- nothing here moves children back itself, because that is
    the recluster's job, honouring the constraint written below.
    """
    if not merge_id:
        return {"error": "missing merge_id"}
    m = conn.execute(f"SELECT * FROM {spec.merge_table} WHERE id=?", (merge_id,)).fetchone()
    if not m:
        return {"error": "merge not found"}
    now = db.now_iso()
    # Deleting the 'same' link is not enough on its own: if the automatic
    # clusterer would group these two back together anyway, they would silently
    # re-merge on the very next pass. So this also writes a 'different'
    # (cannot-link) row over the same pair -- both faces/cluster.py's and
    # pets/cluster.py's `_apply_links` give cannot-link precedence over
    # must-link, and it stays reversible (merging them again writes 'same'
    # back over it via INSERT OR REPLACE).
    #
    # Known limitation: _apply_links only blocks the automatic pass from
    # UNIONING two of its own clusters; it never SPLITS a cluster that pass
    # forms on its own. If the embeddings alone would already place these
    # children together, this cannot-link cannot pull them apart -- that is
    # pre-existing clustering behaviour, not something undo can fix.
    if m["link_a"] and m["link_b"]:
        conn.execute(
            f"DELETE FROM {spec.link_table} WHERE {spec.link_a}=? AND {spec.link_b}=?",
            (m["link_a"], m["link_b"]),
        )
        conn.execute(
            f"INSERT OR REPLACE INTO {spec.link_table}"
            f"({spec.link_a}, {spec.link_b}, kind, created_at) VALUES(?,?,'different',?)",
            (m["link_a"], m["link_b"], now),
        )
    if restore is not None:
        restore(conn, m)
    conn.execute(f"DELETE FROM {spec.merge_table} WHERE id=?", (merge_id,))
    conn.commit()
    return {"ok": True, "recluster": True}
