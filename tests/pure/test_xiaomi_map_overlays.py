"""Pure-tier tests for the xiaomi-JSON-brand map overlays added alongside
Atlas-card support for this model family: carpets, the traveled-path trail,
and the mm->metre scale conversion `vector_map()` needs for this brand.

No homeassistant import — see conftest.py for how `xvac.map`/`map_vector`
get loaded standalone.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import map_vector
from xvac.map import MapFetcher


# --- MapFetcher._parse_carpets --------------------------------------------
def test_parse_carpets_extracts_rectangles():
    blob = json.dumps({"carpets": [
        {"p": [0, 0, 1000, 0, 1000, 1000, 0, 1000], "d": "c1"},
        {"p": [2000, 2000, 2500, 2000, 2500, 2500, 2000, 2500], "d": "c2"},
    ]})
    out = MapFetcher._parse_carpets(blob)
    assert out == [
        [0, 0, 1000, 0, 1000, 1000, 0, 1000],
        [2000, 2000, 2500, 2000, 2500, 2500, 2000, 2500],
    ]


def test_parse_carpets_missing_or_malformed_returns_empty():
    assert MapFetcher._parse_carpets(json.dumps({})) == []
    assert MapFetcher._parse_carpets(json.dumps({"carpets": [{"p": [1, 2]}]})) == []  # short p
    assert MapFetcher._parse_carpets("not json") == []


# --- MapFetcher._parse_path ------------------------------------------------
def _point(x, y, type_=1):
    return {"x": x, "y": y, "type": type_, "sweep_mop_mode": 0, "yaw": 0}


def _path_blob(points):
    return json.dumps({"paths": {"pose_id": 1, "points": json.dumps(points)}})


def test_parse_path_splits_on_type_zero():
    """A `type:0` point starts a brand-new, disconnected segment — never
    joined to whatever came before it (the bug a real user caught: joining
    every point into one line drew straight cuts through walls between
    legs)."""
    points = [
        _point(1, 1, 1), _point(10, 0, 1), _point(20, 0, 1),   # leg 1 (3 pts)
        _point(500, 500, 0), _point(510, 500, 1),               # leg 2 (2 pts, new leg starts here)
    ]
    segments = MapFetcher._parse_path(_path_blob(points))
    assert segments == [
        [(1, 1), (10, 0), (20, 0)],
        [(500, 500), (510, 500)],
    ]


def test_parse_path_strips_zero_sentinel_buffer():
    """The raw array is a fixed-size preallocated buffer; unwritten slots are
    literal (0,0) sentinels forming one contiguous run — keep only the
    longest contiguous run of real (non 0,0) points."""
    sentinels = [_point(0, 0, 0)] * 5
    real = [_point(1, 1, 1), _point(2, 2, 1), _point(3, 3, 1)]
    segments = MapFetcher._parse_path(_path_blob(sentinels + real))
    assert segments == [[(1, 1), (2, 2), (3, 3)]]


def test_parse_path_drops_single_point_segments():
    """A segment needs >=2 points to be drawable as a line."""
    points = [
        _point(1, 1, 1), _point(10, 0, 1),   # real 2-point leg
        _point(999, 999, 0),                  # new leg with only one point ever recorded
    ]
    segments = MapFetcher._parse_path(_path_blob(points))
    assert segments == [[(1, 1), (10, 0)]]


def test_parse_path_missing_or_malformed_returns_empty():
    assert MapFetcher._parse_path(json.dumps({})) == []
    assert MapFetcher._parse_path("not json") == []
    assert MapFetcher._parse_path(json.dumps({"paths": {"points": "not an array"}})) == []


# --- MapFetcher._mm_to_pixel_from_calibration ------------------------------
def test_mm_to_pixel_from_calibration_builds_correct_affine():
    # Three points relating a simple 1:1 vacuum(mm)->map(px) mapping, offset
    # by (100, 200), with the y-axis inverted (as real calibration data is).
    calibration = [
        {"vacuum": {"x": 0, "y": 0}, "map": {"x": 100, "y": 200}},
        {"vacuum": {"x": 10, "y": 0}, "map": {"x": 110, "y": 200}},
        {"vacuum": {"x": 0, "y": 10}, "map": {"x": 100, "y": 190}},
    ]
    transform = MapFetcher._mm_to_pixel_from_calibration(calibration)
    assert transform is not None
    assert transform(0, 0) == (100, 200)
    assert transform(10, 0) == (110, 200)
    assert transform(0, 10) == (100, 190)
    assert transform(5, 5) == (105, 195)


def test_mm_to_pixel_from_calibration_missing_points_returns_none():
    assert MapFetcher._mm_to_pixel_from_calibration([]) is None
    assert MapFetcher._mm_to_pixel_from_calibration([{"vacuum": {"x": 0, "y": 0}, "map": {"x": 0, "y": 0}}]) is None


# --- map_vector.vector_map: scale / carpets / path -------------------------
def _fake_md_with_path():
    room = SimpleNamespace(name="Kitchen", pos_x=1000.0, pos_y=2000.0,
                            x0=0.0, y0=0.0, x1=3000.0, y1=4000.0)
    sub_path = [SimpleNamespace(x=0.0, y=0.0), SimpleNamespace(x=1000.0, y=1000.0)]
    wall = SimpleNamespace(x0=0.0, y0=0.0, x1=1000.0, y1=0.0)
    return SimpleNamespace(
        path=SimpleNamespace(path=[sub_path]),
        charger=SimpleNamespace(x=500.0, y=-500.0), vacuum_position=None,
        goto=None, rooms={3: room}, walls=[wall],
        no_go_areas=[], no_mopping_areas=[], zones=[],
        vacuum_room=None, vacuum_room_name=None,
    )


def test_vector_map_scale_converts_mm_to_metres():
    """xiaomi-brand callers pass scale=0.001 (mm->m) since the card's SVG
    assumes metre-scale constants — confirmed bug: without this, a room's
    bbox came out ~10700x5900 unscaled, a "10km-wide room"."""
    out = map_vector.vector_map(_fake_md_with_path(), b"", ijai_grid=False, scale=0.001)

    assert out["charger"] == {"x": 0.5, "y": -0.5}
    assert out["rooms"][0]["cx"] == 1.0
    assert out["rooms"][0]["cy"] == 2.0
    assert out["rooms"][0]["bbox"] == [0.0, 0.0, 3.0, 4.0]
    assert out["walls"] == [[0.0, 0.0, 1.0, 0.0]]


def test_vector_map_path_param_takes_priority_and_stays_segmented():
    """Explicit `path=` (xiaomi's own segment-split trajectory) wins over
    `md.path`, and the output is always a list of segments — never a flat
    point list (that shape silently drew impossible-looking straight lines
    through walls for both brands before this fix)."""
    explicit_path = [[(0.0, 0.0), (1000.0, 0.0)], [(2000.0, 2000.0), (2500.0, 2000.0)]]
    out = map_vector.vector_map(
        _fake_md_with_path(), b"", ijai_grid=False, scale=0.001, path=explicit_path,
    )
    assert out["path"] == [[[0.0, 0.0], [1.0, 0.0]], [[2.0, 2.0], [2.5, 2.0]]]


def test_vector_map_falls_back_to_md_path_as_segments():
    """When no explicit `path=` is given, `md.path.path` (ijai's own already
    sub-path-shaped data) is used — also scaled, also kept as segments (this
    was a latent bug: previously flattened into one line, same class of bug
    as xiaomi's, just never noticed because ijai already is metre-scale)."""
    out = map_vector.vector_map(_fake_md_with_path(), b"", ijai_grid=False, scale=1.0)
    assert out["path"] == [[[0.0, 0.0], [1000.0, 1000.0]]]


def test_vector_map_carpets_scaled():
    carpets = [[0, 0, 1000, 0, 1000, 1000, 0, 1000]]
    out = map_vector.vector_map(_fake_md_with_path(), b"", ijai_grid=False, scale=0.001, carpets=carpets)
    assert out["carpets"] == [[0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]]


def test_vector_map_no_carpets_key_when_none_given():
    out = map_vector.vector_map(_fake_md_with_path(), b"", ijai_grid=False, scale=0.001)
    assert "carpets" not in out


def test_vector_map_xiaomi_grid_merged_into_output():
    xg = {"size": {"x": 2, "y": 2}, "bounds": {"minX": 0, "minY": 0, "maxX": 1, "maxY": 1},
          "resolution": 0.5, "grid_rle": [0, 4], "legend": {"room_min": 1, "room_max": 6}}
    out = map_vector.vector_map(_fake_md_with_path(), b"", ijai_grid=False, xiaomi_grid=xg)
    assert out["grid_rle"] == [0, 4]
    assert out["legend"]["room_min"] == 1
