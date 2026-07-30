"""Read-only queries backing the GUI. Each opens its own connection so the
handler stays thread-safe under ThreadingHTTPServer.

Everything is scoped to an archive (a row in `roots`, one per isolated
database, see ``archives()`` below). A root_id of None on the other query
functions means "no filter" (harmless: each archive's database only ever
has the one root anyway).
"""

from __future__ import annotations

# Re-export: defined in faces/ now, kept here for call sites not yet repointed.
from ..faces.manual_tags import repair_manual_person_files  # noqa: F401

# Re-export: defined in pets/ now, kept here for call sites not yet repointed.
from ..pets.manual_tags import repair_manual_pet_files  # noqa: F401

# Re-export: defined in services/archives.py now, kept here for call sites not yet repointed.
from ..services.archives import add_archive, archives, remove_archive  # noqa: F401

# Re-export: defined in services/browse.py now, kept here for call sites not yet repointed.
from ..services.browse import (  # noqa: F401
    browse_filters,
    folders,
    item,
    media,
    media_source,
    set_date,
)

# Re-export: defined in services/dups.py now, kept here for call sites not yet repointed.
from ..services.dups import dup_groups, dup_summary  # noqa: F401

# Re-export: defined in services/overview.py now, kept here for call sites not yet repointed.
from ..services.overview import date_sources, summary, timeline  # noqa: F401

# Re-export: defined in services/pending.py now, kept here for call sites not yet repointed.
from ..services.pending import detect_pending, faces_pending, pets_pending  # noqa: F401

# Re-export: defined in services/people.py now, kept here for call sites not yet repointed.
from ..services.people import (  # noqa: F401
    add_person_to_file,
    detach_file_from_person,
    face_crop_source,
    face_person,
    face_persons,
    face_summary,
    hide_person,
    merge_persons,
    person_suggestions,
    reassign_face,
    remove_person_from_file,
    rename_person,
    set_persons_different,
    set_persons_skip,
    unmerge_persons,
)

# Re-export: defined in services/pets.py now, kept here for call sites not yet repointed.
from ..services.pets import (  # noqa: F401
    add_pet_to_file,
    animal_crop_source,
    animal_gallery,
    merge_pets,
    nonhuman_review,
    pet_group,
    pet_groups,
    pet_summary,
    remove_pet_from_file,
    rename_pet,
    review_nonhuman,
    unmerge_pets,
)

# Re-export: defined in services/places.py now, kept here for call sites not yet repointed.
from ..services.places import (  # noqa: F401
    clear_place,
    create_place,
    merge_place_clusters,
    place_cluster_members,
    place_clusters,
    place_merge_preview,
    place_points,
    recompute_place_clusters,
    rename_place_cluster,
    set_place,
    unmerge_place_clusters,
)

# Re-export: defined in services/search.py now, kept here for call sites not yet repointed.
from ..services.search import (  # noqa: F401
    ALTERNATE_VECTOR_PENALTY,
    semantic_pending,
    semantic_search,
    semantic_summary,
)
