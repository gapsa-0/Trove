"""The embedder migration must not lose hand-made identity decisions."""

from __future__ import annotations

import factories

from trove.config import Config
from trove.faces import migrate_adaface as mig


def _catalog(tmp_path):
    conn = factories.make_db(tmp_path)
    factories.add_files(conn, 2)
    factories.add_person(conn, name="Mari", person_id=1)
    # Two faces of Mari, one flagged doll, one ordinary auto-clustered face.
    factories.add_face(
        conn, file_id=1, face_id=10, box=(100, 100, 60, 60), det_score=0.9, person_id=1
    )
    factories.add_face(
        conn,
        file_id=2,
        face_id=11,
        box=(20, 20, 50, 50),
        det_score=0.8,
        person_id=1,
        manual_person="Mari",
    )
    factories.add_face(
        conn,
        file_id=1,
        face_id=12,
        box=(300, 300, 40, 40),
        det_score=0.7,
        not_person=1,
        nonhuman_kind="toy",
        nonhuman_source="manual",
    )
    conn.execute(
        """INSERT INTO face_links(face_a,face_b,kind,created_at)
           VALUES(10,11,'same','2026-01-01')"""
    )
    conn.commit()
    return conn


def _redetect(conn, boxes):
    """Stand in for the re-extract: fresh face rows with new ids, same boxes."""
    for file_id, (x, y, w, h) in boxes:
        factories.add_face(conn, file_id=file_id, box=(x, y, w, h), det_score=0.9)
    conn.commit()


def test_names_pins_flags_and_links_survive_the_re_extract(tmp_path):
    cfg = Config()
    conn = _catalog(tmp_path)

    st = mig.snapshot_and_wipe(conn, cfg)
    # Only identity-bearing faces are carried; the plain auto face needs none.
    assert st.faces_snapshotted == 3
    assert st.links_snapshotted == 1
    assert conn.execute("SELECT COUNT(*) FROM faces").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM face_scan").fetchone()[0] == 0

    # Re-detection reproduces the same boxes, shifted by a pixel of rounding.
    _redetect(conn, [(1, (101, 100, 60, 60)), (2, (20, 21, 50, 50)), (1, (300, 300, 40, 40))])

    rst = mig.reattach(conn, cfg)
    assert rst.faces_reattached == 3
    assert rst.unmatched == 0
    assert rst.links_restored == 1

    rows = {
        (r["box_x"], r["box_y"]): r
        for r in conn.execute(
            "SELECT box_x, box_y, manual_person, not_person, nonhuman_kind FROM faces"
        )
    }
    # The person's NAME comes back as a pin, which is what clustering honours.
    assert rows[(101, 100)]["manual_person"] == "Mari"
    assert rows[(20, 21)]["manual_person"] == "Mari"
    assert rows[(300, 300)]["not_person"] == 1
    assert rows[(300, 300)]["nonhuman_kind"] == "toy"

    link = conn.execute("SELECT face_a, face_b, kind FROM face_links").fetchone()
    new_ids = {
        r["id"]: (r["box_x"], r["box_y"])
        for r in conn.execute("SELECT id, box_x, box_y FROM faces")
    }
    assert {new_ids[link["face_a"]], new_ids[link["face_b"]]} == {(101, 100), (20, 21)}
    conn.close()


def test_links_between_otherwise_plain_faces_survive(tmp_path):
    """Regression: a link endpoint carrying nothing else must still be carried.

    face_links is remapped *through* the carry table, so on a real archive —
    where clusters are unnamed and links come from "same person?" review — every
    link was silently dropped when only named/pinned faces were snapshotted.
    """
    cfg = Config()
    conn = _catalog(tmp_path)
    # Strip every other identity marker; only the link remains.
    conn.execute(
        "UPDATE faces SET person_id=NULL, manual_person=NULL, not_person=0, nonhuman_kind=NULL"
    )
    conn.execute("DELETE FROM persons")
    conn.commit()

    st = mig.snapshot_and_wipe(conn, cfg)
    assert st.faces_snapshotted == 2, "link endpoints were not carried"

    _redetect(conn, [(1, (100, 100, 60, 60)), (2, (20, 20, 50, 50))])
    rst = mig.reattach(conn, cfg)
    assert rst.links_restored == 1
    assert rst.links_dropped == 0
    assert conn.execute("SELECT COUNT(*) FROM face_links").fetchone()[0] == 1
    conn.close()


def test_a_face_the_detector_no_longer_finds_drops_its_link(tmp_path):
    """Half a link is meaningless, so it is dropped rather than guessed at."""
    cfg = Config()
    conn = _catalog(tmp_path)
    mig.snapshot_and_wipe(conn, cfg)
    _redetect(conn, [(1, (100, 100, 60, 60))])  # face 11 never comes back

    rst = mig.reattach(conn, cfg)
    assert rst.faces_reattached == 1
    assert rst.links_dropped == 1
    assert conn.execute("SELECT COUNT(*) FROM face_links").fetchone()[0] == 0
    conn.close()


def test_a_box_that_moved_too_far_is_not_treated_as_the_same_face(tmp_path):
    cfg = Config()
    conn = _catalog(tmp_path)
    mig.snapshot_and_wipe(conn, cfg)
    _redetect(conn, [(1, (100, 100, 60, 60)), (2, (400, 400, 50, 50))])

    rst = mig.reattach(conn, cfg)
    assert rst.faces_reattached == 1  # only the box that really matches
    moved = conn.execute("SELECT manual_person FROM faces WHERE box_x=400").fetchone()
    assert moved["manual_person"] is None, "a pin landed on an unrelated face"
    conn.close()


def test_reattach_is_idempotent(tmp_path):
    cfg = Config()
    conn = _catalog(tmp_path)
    mig.snapshot_and_wipe(conn, cfg)
    _redetect(conn, [(1, (100, 100, 60, 60)), (2, (20, 20, 50, 50)), (1, (300, 300, 40, 40))])
    first = mig.reattach(conn, cfg)
    second = mig.reattach(conn, cfg)
    assert first.faces_reattached == second.faces_reattached
    assert conn.execute("SELECT COUNT(*) FROM face_links").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE manual_person='Mari'").fetchone()[0] == 2
    conn.close()


def test_pending_reports_whether_the_migration_still_owes_a_reattach(tmp_path):
    cfg = Config()
    conn = _catalog(tmp_path)
    assert mig.pending(conn) is False
    mig.snapshot_and_wipe(conn, cfg)
    assert mig.pending(conn) is True
    _redetect(conn, [(1, (100, 100, 60, 60)), (2, (20, 20, 50, 50)), (1, (300, 300, 40, 40))])
    mig.reattach(conn, cfg)
    assert mig.pending(conn) is False
    conn.close()
