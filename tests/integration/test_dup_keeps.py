"""Choosing which copies of a duplicate group Browse shows.

Trove picks one -- the biggest, best-provenanced copy -- and hides the rest.
That is a good default and a bad rule: the "worse" copy can be the one already
in the album that gets shared, and two copies of what the grouping called the
same picture are sometimes two pictures. So the choice is the user's, with one
thing they cannot do, which is leave a group showing nothing at all.

The case that makes this non-trivial is that `dup_groups` and `dup_members` are
deleted and rebuilt from scratch by every grouping run, so a choice recorded
against a group would last until the next batch of photos arrived. These check
it against a real re-run, not against the row it just wrote.
"""

from trove.config import Config
from trove.db import database as db
from trove.dedup import exact
from trove.services import dups, dups_edit


def _archive_with_a_group(tmp_path):
    """Three byte-identical copies of one photo, grouped for real.

    Run through `exact.run` rather than hand-written rows, so the group,
    its canonical and every `files.hidden` are whatever the pass actually
    produces -- which is the thing the choice below has to survive.
    """
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id in (1, 2, 3):
        conn.execute(
            """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                                 sha256,first_seen,last_seen)
               VALUES(?,1,?,'jpg',100,0,'image',?,'2026-01-01','2026-01-01')""",
            (file_id, f"{file_id}.jpg", "a" * 64),
        )
    conn.commit()
    exact.run(conn, Config.load(), root_id=1)
    return db_path, conn


def _visible(conn):
    return {r[0] for r in conn.execute("SELECT id FROM files WHERE hidden=0")}


def _canonical(conn):
    return conn.execute("SELECT canonical_file_id FROM dup_groups").fetchone()[0]


def test_by_default_one_copy_is_shown(tmp_path):
    """The guard on everything below: without a choice, nothing changes."""
    _, conn = _archive_with_a_group(tmp_path)
    assert _visible(conn) == {_canonical(conn)}
    conn.close()


def test_keeping_two_copies_shows_both(tmp_path):
    """What the whole feature is: two copies the grouping called the same thing,
    kept as two things."""
    db_path, conn = _archive_with_a_group(tmp_path)
    canonical = _canonical(conn)
    other = next(iter({1, 2, 3} - {canonical}))
    conn.close()

    assert dups_edit.set_kept_copies(str(db_path), 1, [canonical, other])["ok"] is True

    conn = db.connect(db_path)
    assert _visible(conn) == {canonical, other}
    conn.close()


def test_the_copy_trove_picked_can_itself_be_hidden(tmp_path):
    """The default is a ranking, not a verdict, and the user may disagree with
    it outright rather than only adding to it."""
    db_path, conn = _archive_with_a_group(tmp_path)
    canonical = _canonical(conn)
    other = next(iter({1, 2, 3} - {canonical}))
    conn.close()

    dups_edit.set_kept_copies(str(db_path), 1, [other])

    conn = db.connect(db_path)
    assert _visible(conn) == {other}
    conn.close()


def test_a_group_cannot_be_left_showing_nothing(tmp_path):
    """A group with every copy hidden is a picture missing from Browse with
    nothing on any screen to say where it went."""
    db_path, _ = _archive_with_a_group(tmp_path)

    assert "error" in dups_edit.set_kept_copies(str(db_path), 1, [])
    assert "error" in dups_edit.set_kept_copies(str(db_path), 1, None)


def test_a_file_from_another_group_is_refused(tmp_path):
    """The set is the group's members, and being handed something else is a bug
    in the caller rather than an instruction to widen the group."""
    db_path, conn = _archive_with_a_group(tmp_path)
    conn.execute(
        """INSERT INTO files(id,root_id,rel_path,ext,size,mtime,media_type,
                             sha256,first_seen,last_seen)
           VALUES(9,1,'elsewhere.jpg','jpg',100,0,'image','b',
                  '2026-01-01','2026-01-01')"""
    )
    conn.commit()
    conn.close()

    assert "error" in dups_edit.set_kept_copies(str(db_path), 1, [9])


def test_the_choice_survives_the_next_grouping_run(tmp_path):
    """The point of recording it against files rather than against the group:
    grouping deletes and rebuilds every group it has."""
    db_path, conn = _archive_with_a_group(tmp_path)
    canonical = _canonical(conn)
    other = next(iter({1, 2, 3} - {canonical}))
    conn.close()

    dups_edit.set_kept_copies(str(db_path), 1, [other])

    conn = db.connect(db_path)
    exact.run(conn, Config.load(), root_id=1)
    assert _visible(conn) == {other}, "the rebuild put the automatic pick back"
    conn.close()


def test_putting_it_back_the_way_it_was_stops_overriding(tmp_path):
    """Choosing exactly what Trove chose is not a choice to keep: a later
    regroup that picks a different canonical should be followed, not fought."""
    db_path, conn = _archive_with_a_group(tmp_path)
    canonical = _canonical(conn)
    conn.close()

    dups_edit.set_kept_copies(str(db_path), 1, [canonical])

    conn = db.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM dup_keeps").fetchone()[0] == 0
    conn.close()


def test_what_is_reclaimable_follows_what_is_hidden(tmp_path):
    """Keeping a second copy means its bytes are no longer going spare, and the
    screen offers them back until it is told so."""
    db_path, conn = _archive_with_a_group(tmp_path)
    canonical = _canonical(conn)
    other = next(iter({1, 2, 3} - {canonical}))
    before = dups.dup_summary(str(db_path), root_id=1)["reclaimable"]
    conn.close()

    dups_edit.set_kept_copies(str(db_path), 1, [canonical, other])

    after = dups.dup_summary(str(db_path), root_id=1)["reclaimable"]
    assert (before, after) == (200, 100)


def test_the_listing_says_which_copies_are_kept(tmp_path):
    """The screen draws a toggle per copy, so it has to be told the state of
    each of them, not just which one was canonical."""
    db_path, conn = _archive_with_a_group(tmp_path)
    canonical = _canonical(conn)
    other = next(iter({1, 2, 3} - {canonical}))
    conn.close()

    dups_edit.set_kept_copies(str(db_path), 1, [canonical, other])

    members = dups.dup_groups(str(db_path), root_id=1)["groups"][0]["members"]
    kept = {m["id"] for m in members if m["kept"]}
    assert kept == {canonical, other}
    # Kept first, so a row of pictures reads as "these are shown, these are not".
    assert [m["kept"] for m in members] == [True, True, False]
