import json

from organize_archive.metadata.takeout import SidecarMatcher, parse_sidecar


def _write(d, name, taken=1652519397, lat=0.0, lon=0.0, title=None):
    (d / name).write_text(
        json.dumps(
            {
                "title": title or name.replace(".json", "").replace(".supplemental-metadata", ""),
                "description": "",
                "photoTakenTime": {"timestamp": str(taken)},
                "geoData": {"latitude": lat, "longitude": lon, "altitude": 0.0},
                "geoDataExif": {"latitude": lat, "longitude": lon, "altitude": 0.0},
            }
        )
    )


def test_exact_match(tmp_path):
    (tmp_path / "IMG_1.jpg").write_bytes(b"x")
    _write(tmp_path, "IMG_1.jpg.json")
    m = SidecarMatcher().find(tmp_path / "IMG_1.jpg")
    assert m is not None and m[1] == "exact"


def test_supplemental_metadata(tmp_path):
    (tmp_path / "IMG_2.jpg").write_bytes(b"x")
    _write(tmp_path, "IMG_2.jpg.supplemental-metadata.json")
    m = SidecarMatcher().find(tmp_path / "IMG_2.jpg")
    assert m is not None and m[1] == "supplemental"


def test_counter_shift(tmp_path):
    # media IMG_3(1).jpg  ->  IMG_3.jpg(1).json
    (tmp_path / "IMG_3(1).jpg").write_bytes(b"x")
    _write(tmp_path, "IMG_3.jpg(1).json")
    m = SidecarMatcher().find(tmp_path / "IMG_3(1).jpg")
    assert m is not None and m[1] == "counter"


def test_edited_reuses_original(tmp_path):
    (tmp_path / "IMG_4-edited.jpg").write_bytes(b"x")
    _write(tmp_path, "IMG_4.jpg.json")
    m = SidecarMatcher().find(tmp_path / "IMG_4-edited.jpg")
    assert m is not None and m[1] == "edited"


def test_truncated_prefix(tmp_path):
    long = "a_very_long_original_filename_from_google_photos.jpg"
    (tmp_path / long).write_bytes(b"x")
    _write(tmp_path, "a_very_long_original_filename_from_go.json")
    m = SidecarMatcher().find(tmp_path / long)
    assert m is not None and m[1] == "prefix-trunc"


def test_no_sidecar(tmp_path):
    (tmp_path / "orphan.jpg").write_bytes(b"x")
    assert SidecarMatcher().find(tmp_path / "orphan.jpg") is None


def test_parse_zero_location_is_none(tmp_path):
    _write(tmp_path, "p.jpg.json", lat=0.0, lon=0.0)
    data = parse_sidecar(tmp_path / "p.jpg.json")
    assert data.lat is None and data.lon is None
    assert data.taken_time == 1652519397


def test_parse_real_location(tmp_path):
    _write(tmp_path, "q.jpg.json", lat=-41.13, lon=-71.31)
    data = parse_sidecar(tmp_path / "q.jpg.json")
    assert data.lat == -41.13 and data.lon == -71.31
