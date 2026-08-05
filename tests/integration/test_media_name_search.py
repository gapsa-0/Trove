"""Browse's search box on an archive with no search feature: matching names.

Nothing here is indexed, embedded or read -- the names came off the disk during
the scan -- so these tests are what stand behind the promise that every archive
can be searched for something.
"""

from trove.db import database as db
from trove.services import browse

_FILES = (
    (1, "vacaciones/playa 2019.jpg"),
    (2, "vacaciones/PLAYA-atardecer.jpg"),
    (3, "papeles/escritura_2019.pdf"),
    (4, "recibo.pdf"),
    (5, "papeles/playa/nota.txt"),
)


def _named_catalogue(tmp_path):
    db_path = tmp_path / "archive.db"
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.execute("INSERT INTO roots(id,path,added_at) VALUES(1,'/photos','2026-01-01')")
    for file_id, rel_path in _FILES:
        conn.execute(
            """INSERT INTO files(
                   id,root_id,rel_path,size,mtime,media_type,first_seen,last_seen
               ) VALUES(?,1,?,1,0,'image','2026-01-01','2026-01-01')""",
            (file_id, rel_path),
        )
        conn.execute(
            "INSERT INTO dates(file_id,best_datetime) VALUES(?,'2026-01-01')",
            (file_id,),
        )
    conn.commit()
    conn.close()
    return db_path


def test_name_search_matches_part_of_a_name_whatever_the_case(tmp_path):
    db_path = _named_catalogue(tmp_path)

    result = browse.media(db_path, root_id=1, name="playa")

    assert [item["id"] for item in result["items"]] == [1, 2]
    assert result["total"] == 2


def test_name_search_ignores_the_folders_a_file_sits_in(tmp_path):
    """File 5 is *in* a folder called playa; its own name says nothing about it.

    Matching the whole relative path would make a single badly named folder
    answer every search made inside it."""
    db_path = _named_catalogue(tmp_path)

    result = browse.media(db_path, root_id=1, name="playa")

    assert 5 not in [item["id"] for item in result["items"]]


def test_every_typed_word_has_to_appear_in_the_name(tmp_path):
    """Typing more narrows: the words are ANDed, and their order is not the
    order the file was named in."""
    db_path = _named_catalogue(tmp_path)

    both = browse.media(db_path, root_id=1, name="2019 playa")
    neither = browse.media(db_path, root_id=1, name="playa escritura")

    assert [item["id"] for item in both["items"]] == [1]
    assert neither["items"] == [] and neither["total"] == 0


def test_punctuation_in_a_name_search_is_matched_literally(tmp_path):
    """`_` is a LIKE wildcard and a perfectly ordinary character in a filename.
    Searched as itself, "escritura_2019" is one file rather than a pattern."""
    db_path = _named_catalogue(tmp_path)

    underscore = browse.media(db_path, root_id=1, name="escritura_2019")
    wildcarded = browse.media(db_path, root_id=1, name="escritura_2020")

    assert [item["id"] for item in underscore["items"]] == [3]
    assert wildcarded["items"] == []


def test_an_empty_name_search_is_not_a_filter(tmp_path):
    """Whitespace is not a search, and must not empty the grid."""
    db_path = _named_catalogue(tmp_path)

    for query in (None, "", "   "):
        result = browse.media(db_path, root_id=1, name=query)
        assert result["total"] == len(_FILES)


def test_name_search_composes_with_the_other_filters(tmp_path):
    """It narrows the same listing the filter bar narrows, rather than
    replacing it -- so a search inside a year is still a search inside it."""
    db_path = _named_catalogue(tmp_path)
    conn = db.connect(db_path)
    conn.execute("UPDATE dates SET best_datetime='2019-07-04' WHERE file_id=1")
    conn.commit()
    conn.close()

    result = browse.media(db_path, root_id=1, name="playa", year="2019")

    assert [item["id"] for item in result["items"]] == [1]
    assert result["total"] == 1
