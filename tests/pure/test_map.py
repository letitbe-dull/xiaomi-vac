"""Pure-tier tests for per-brand map decode.

No homeassistant import — runs on native Windows. Covers:
  - the ijai labelled-grid vector contract (`map_vector.extract_grid` /
    `vector_map`), the part that's brand-specific;
  - the xiaomi JSON-map grid contract (`map_vector.extract_json_grid`), the
    second source of true room contours;
  - the brand dispatch (`map_parsers`) that picks the right parser + unpack
    inputs + grid flag per brand.

The repo's `current_map.bin` is an AES-encrypted ijai blob whose key inputs
(wifi_sn/owner_id/device_id/mac) are device secrets not stored here, so it can't
be decrypted in a unit test. Instead we build a synthetic ijai `RobotMap`
protobuf — the exact shape `extract_grid` consumes — so the contract is exercised
end to end without secrets. Per-brand fixtures get added as real blobs appear.
"""
from __future__ import annotations

import base64
import json
import zlib
from types import SimpleNamespace

import pytest

# Imported standalone (not via the HA-importing package __init__) — see conftest.
import map_parsers
import map_vector


# --- synthetic ijai blob -------------------------------------------------
def _build_ijai_blob(grid: bytes, w: int, h: int):
    """Serialize a minimal ijai RobotMap protobuf carrying a labelled grid."""
    import vacuum_map_parser_ijai.RobotMap_pb2 as RobotMap

    rm = RobotMap.RobotMap()
    rm.mapHead.mapHeadId = 42
    rm.mapHead.sizeX = w
    rm.mapHead.sizeY = h
    rm.mapHead.minX = -2.0
    rm.mapHead.minY = -2.0
    rm.mapHead.maxX = 2.0
    rm.mapHead.maxY = 2.0
    rm.mapHead.resolution = 0.05
    rm.mapData.mapData = grid
    rm.chargeStation.x = 0.5
    rm.chargeStation.y = -0.5
    return rm.SerializeToString()


def _grid_4x4_with_room():
    """4x4 grid, room id 10 in the centre 2x2 block, rest outside (0)."""
    g = bytearray(16)
    for c, r in ((1, 1), (2, 1), (1, 2), (2, 2)):
        g[r * 4 + c] = 10
    return bytes(g), 4, 4


def _expand_rle(rle: list[int]) -> bytes:
    out = bytearray()
    for i in range(0, len(rle), 2):
        out.extend([rle[i]] * rle[i + 1])
    return bytes(out)


# --- extract_grid (ijai labelled grid) -----------------------------------
def test_extract_grid_roundtrip():
    grid, w, h = _grid_4x4_with_room()
    out = map_vector.extract_grid(_build_ijai_blob(grid, w, h))

    assert out["map_id"] == 42
    assert out["size"] == {"x": 4, "y": 4}
    assert out["bounds"] == {"minX": -2.0, "minY": -2.0, "maxX": 2.0, "maxY": 2.0}
    # the RLE losslessly encodes the row-major grid
    assert _expand_rle(out["grid_rle"]) == grid
    # room 10 is traced from the grid into a closed ring
    ids = {chain["id"] for chain in out["room_chains"]}
    assert 10 in ids
    assert out["legend"]["room_min"] == 10


def test_extract_grid_falls_back_to_room_chain():
    """A grid with no labelled rooms uses the firmware roomChain instead."""
    import vacuum_map_parser_ijai.RobotMap_pb2 as RobotMap

    rm = RobotMap.RobotMap()
    rm.mapHead.sizeX = 2
    rm.mapHead.sizeY = 2
    rm.mapData.mapData = bytes(4)  # all outside -> no labelled rooms
    chain = rm.roomChain.add()
    chain.roomId = 7
    for x, y in ((0, 0), (1, 0), (1, 1)):
        p = chain.points.add()
        p.x, p.y = x, y

    out = map_vector.extract_grid(rm.SerializeToString())
    assert out["room_chains"] == [{"id": 7, "rings": [[[0, 0], [1, 0], [1, 1]]]}]


# --- vector_map (grid + md overlays) -------------------------------------
def _fake_md():
    """Minimal duck-typed MapData: a charger + one named room, nothing else."""
    room = SimpleNamespace(name="Kitchen", pos_x=0.1, pos_y=0.2,
                           x0=0.0, y0=0.0, x1=1.0, y1=1.0)
    return SimpleNamespace(
        path=None, charger=SimpleNamespace(x=0.5, y=-0.5), vacuum_position=None,
        goto=None, rooms={10: room}, walls=[], no_go_areas=[], no_mopping_areas=[],
        zones=[], vacuum_room=None, vacuum_room_name=None,
    )


def test_vector_map_ijai_has_grid():
    grid, w, h = _grid_4x4_with_room()
    out = map_vector.vector_map(_fake_md(), _build_ijai_blob(grid, w, h), ijai_grid=True)

    assert out["grid_rle"]  # real grid present
    assert out["map_id"] == 42
    assert out["charger"] == {"x": 0.5, "y": -0.5}
    assert out["rooms"][0]["name"] == "Kitchen"


def _fake_md_mm():
    """MapData as `vacuum_map_parser_xiaomi` reports it: every length in mm.

    Mirrors the live xiaomi.vacuum.ov71gl payload (S40 Pro): rooms metres away
    from the origin expressed in millimetres, dock near the origin.
    """
    room = SimpleNamespace(name="Bedroom", pos_x=-1305.0, pos_y=-4989.0,
                           x0=-2750.0, y0=-6550.0, x1=400.0, y1=-3750.0)
    return SimpleNamespace(
        path=SimpleNamespace(path=[[SimpleNamespace(x=100.0, y=-200.0)]]),
        charger=SimpleNamespace(x=331.0, y=17.0),
        vacuum_position=SimpleNamespace(x=181.0, y=16.0),
        goto=None, rooms={5: room},
        walls=[SimpleNamespace(x0=0.0, y0=0.0, x1=1000.0, y1=2000.0)],
        no_go_areas=[], no_mopping_areas=[],
        zones=[SimpleNamespace(x0=0.0, y0=0.0, x1=1000.0, y1=2000.0)],
        vacuum_room=None, vacuum_room_name=None,
    )


def test_vector_map_scales_millimetre_overlays_to_metres():
    """The xiaomi JSON family reports mm; the card contract is metres.

    Unscaled, a 3-metre room spans ~3000 units, so the card's absolute label
    and marker sizes (font-size 0.42, dock r=0.32) render 1000x too small.
    """
    out = map_vector.vector_map(
        _fake_md_mm(), b"", ijai_grid=False, units_per_metre=1000.0)

    assert out["charger"] == {"x": 0.331, "y": 0.017}
    assert out["vacuum"] == {"x": 0.181, "y": 0.016}
    assert out["rooms"][0]["bbox"] == [-2.75, -6.55, 0.4, -3.75]
    assert out["rooms"][0]["cx"] == -1.305
    assert out["rooms"][0]["cy"] == -4.989
    assert out["path"] == [[0.1, -0.2]]
    assert out["walls"] == [[0.0, 0.0, 1.0, 2.0]]
    assert out["zones"] == [[0.0, 0.0, 1.0, 2.0]]


def test_vector_map_defaults_to_no_scaling():
    """Brands already in metres must pass through byte-identical."""
    unscaled = map_vector.vector_map(_fake_md_mm(), b"", ijai_grid=False)

    assert unscaled["charger"] == {"x": 331.0, "y": 17.0}
    assert unscaled["rooms"][0]["bbox"] == [-2750.0, -6550.0, 400.0, -3750.0]


def test_vector_map_keeps_absent_room_label_position_absent():
    """`Room.pos_x`/`pos_y` are optional; None must not become 0.0, which the
    card would draw as a label stranded at the origin."""
    md = _fake_md_mm()
    md.rooms[5].pos_x = None
    md.rooms[5].pos_y = None

    out = map_vector.vector_map(md, b"", ijai_grid=False, units_per_metre=1000.0)

    assert out["rooms"][0]["cx"] is None
    assert out["rooms"][0]["cy"] is None


def test_overlay_units_per_metre_only_scales_xiaomi_json_family():
    """ijai/dreame/viomi/roidmi parsers already report metres."""
    assert map_parsers.overlay_units_per_metre("xiaomi") == 1000.0
    for brand in ("ijai", "dreame", "viomi", "roidmi"):
        assert map_parsers.overlay_units_per_metre(brand) == 1.0


def test_vector_map_non_ijai_empty_grid():
    """Non-ijai brands carry overlays but no labelled grid (best-effort)."""
    out = map_vector.vector_map(_fake_md(), b"", ijai_grid=False)

    assert out["grid_rle"] == []
    assert out["room_chains"] == []
    assert out["map_id"] is None
    # overlays still come through so the card can draw on the PNG
    assert out["charger"] == {"x": 0.5, "y": -0.5}
    assert out["rooms"][0]["name"] == "Kitchen"


# --- xiaomi JSON-map grid ------------------------------------------------
# Built from a hand-written 6x5 ASCII grid, not a real floor plan: the live
# ov71gl dump that proved this decode is a private home layout and stays out
# of the repo, exactly like the ijai blob above.
def _json_payload(rows, *, room_info=None, resolution=50, origin=(-100, -200)):
    """A reduced xiaomi JSON map payload built from an ASCII grid.

    `rows[0]` is grid row 0 == `origin_y`, i.e. the row index grows NORTH —
    the convention the real payload uses. Each character is a raw JSON cell
    value: "0" outside, "1" floor, digits 3-9 room grid_ids, "w" a wall.
    """
    w, h = len(rows[0]), len(rows)
    cells = bytes(255 if ch == "w" else int(ch) for row in rows for ch in row)
    payload = {
        "width": w, "height": h, "resolution": resolution,
        "origin_x": origin[0], "origin_y": origin[1],
        "map_data": base64.b64encode(zlib.compress(cells)).decode(),
    }
    if room_info is not None:
        payload["map_room_info"] = [{"grid_id": g, "room_id": r}
                                    for g, r in room_info.items()]
    return json.dumps(payload)


# room 3 is an L of 5 cells, room 4 a 2x2 block
_JSON_ROWS = (
    "000000",
    "033000",
    "033440",
    "003440",
    "000000",
)


def _ring_area(ring):
    """Polygon area of a traced ring, in whole cells (shoelace)."""
    s = 0.0
    for i, (x0, y0) in enumerate(ring):
        x1, y1 = ring[(i + 1) % len(ring)]
        s += x0 * y1 - x1 * y0
    return abs(s) / 2


def test_extract_json_grid_traces_rooms_from_the_pixel_grid():
    """Contours come from the base64+zlib `map_data` cells, not from a bbox."""
    out = map_vector.extract_json_grid(_json_payload(_JSON_ROWS),
                                       units_per_metre=1000.0)

    chains = {c["id"]: c["rings"] for c in out["room_chains"]}
    assert set(chains) == {3, 4}
    # An L of 5 cells: its bounding box would be 6, so the traced area proves
    # the outline follows the real cells.
    assert [_ring_area(r) for r in chains[3]] == [5.0]
    assert [_ring_area(r) for r in chains[4]] == [4.0]
    # millimetre payload -> the metre contract, same divisor as the overlays
    assert out["resolution"] == 0.05
    assert out["bounds"]["minX"] == pytest.approx(-0.1)
    assert out["bounds"]["minY"] == pytest.approx(-0.2)
    assert out["bounds"]["maxX"] == pytest.approx(0.2)
    assert out["bounds"]["maxY"] == pytest.approx(0.05)


def test_extract_json_grid_chains_land_on_the_room_bbox():
    """Chain vertices are grid-line (col,row) pairs the card turns into metres
    as `minX + col * resolution`; that must reproduce the room's own extent."""
    out = map_vector.extract_json_grid(_json_payload(_JSON_ROWS),
                                       units_per_metre=1000.0)
    b, res = out["bounds"], out["resolution"]
    room4 = next(c for c in out["room_chains"] if c["id"] == 4)
    xs = [b["minX"] + c * res for ring in room4["rings"] for c, _ in ring]
    ys = [b["minY"] + r * res for ring in room4["rings"] for _, r in ring]
    # cells (3,2)..(4,3): the outline runs along grid lines 3..5 and 2..4
    assert min(xs) == pytest.approx(0.05)
    assert max(xs) == pytest.approx(0.15)
    assert min(ys) == pytest.approx(-0.1)
    assert max(ys) == pytest.approx(0.0)


def test_extract_json_grid_emits_the_raster_only_for_card_room_ids():
    """The card reads raster cells with room_min..room_max hard-coded and then
    looks the label up in `rooms` by id, so the grid may ship only when the
    real room ids are inside that band. ov71gl's 3-7 are not: chains only."""
    bare = map_vector.extract_json_grid(_json_payload(_JSON_ROWS))
    assert bare["room_chains"]            # contours either way
    assert bare["grid_rle"] == []
    assert bare["size"] is None

    mapped = map_vector.extract_json_grid(
        _json_payload(_JSON_ROWS, room_info={3: 11, 4: 12}))
    assert {c["id"] for c in mapped["room_chains"]} == {11, 12}
    assert mapped["size"] == {"x": 6, "y": 5}
    grid = _expand_rle(mapped["grid_rle"])
    assert len(grid) == 30
    # cells carry the ROOM id (what `rooms[].id` and clean_segment use), not
    # the raw grid_id
    assert grid[1 * 6 + 1] == 11
    assert grid[2 * 6 + 3] == 12


def test_extract_json_grid_normalises_cells_to_the_legend():
    """JSON alphabet (0 / 1-2 / 3-63 / >63) -> the legend the card shares."""
    out = map_vector.extract_json_grid(
        _json_payload(("01w0", "0330", "0330", "0000"), room_info={3: 10}))

    grid = _expand_rle(out["grid_rle"])
    assert grid[0] == out["legend"]["outside"]
    assert grid[1] == out["legend"]["floor"]
    assert grid[2] == out["legend"]["wall"]
    assert grid[1 * 4 + 1] == 10


@pytest.mark.parametrize("payload", [
    "not json at all",
    json.dumps({"width": 4, "height": 4}),                         # no map_data
    json.dumps({"width": 4, "height": 4, "map_data": "!!"}),       # not b64+zlib
    json.dumps({"width": 4, "height": 4,                           # short buffer
                "map_data": base64.b64encode(zlib.compress(b"\x00")).decode()}),
    json.dumps({"width": 4, "height": 4,                           # no room cells
                "map_data": base64.b64encode(zlib.compress(bytes(16))).decode()}),
])
def test_extract_json_grid_falls_back_to_the_empty_contract(payload):
    """A cloud payload is external input: every malformed shape degrades to the
    overlays-only contract instead of killing the whole map parse."""
    out = map_vector.extract_json_grid(payload)

    assert out["room_chains"] == []
    assert out["grid_rle"] == []
    assert out["size"] is None
    assert out["bounds"] is None


def test_extract_json_grid_invalid_geometry_falls_back_to_the_empty_contract():
    payload = json.loads(_json_payload(_JSON_ROWS))
    payload["resolution"] = "invalid"
    payload["map_room_info"] = {"not": "a list"}

    out = map_vector.extract_json_grid(payload, units_per_metre=1000.0)

    assert out["room_chains"] == []
    assert out["bounds"] is None


def test_vector_map_xiaomi_json_grid_has_room_chains():
    """The ov71gl path: contours from the payload grid, overlays still metres."""
    out = map_vector.vector_map(
        _fake_md_mm(), _json_payload(_JSON_ROWS), ijai_grid=False,
        json_grid=True, units_per_metre=1000.0)

    assert {c["id"] for c in out["room_chains"]} == {3, 4}
    assert out["resolution"] == 0.05
    assert out["charger"] == {"x": 0.331, "y": 0.017}
    # a blob-embedded map id stays ijai-only: the coordinator trusts it as
    # ground truth when resolving which physical map a cycle belongs to
    assert out["map_id"] is None


def test_vector_map_json_grid_is_off_by_default():
    """Brands without a JSON grid keep the overlays-only contract untouched."""
    out = map_vector.vector_map(_fake_md_mm(), _json_payload(_JSON_ROWS),
                                ijai_grid=False, units_per_metre=1000.0)

    assert out["room_chains"] == []
    assert out["bounds"] is None
    assert out["resolution"] is None


def test_has_json_grid_only_xiaomi():
    """Only the xiaomi JSON-map family carries a decodable pixel grid."""
    assert map_parsers.has_json_grid("xiaomi") is True
    for brand in ("ijai", "dreame", "viomi", "roidmi"):
        assert map_parsers.has_json_grid(brand) is False


# --- brand dispatch ------------------------------------------------------
def _profile(brand: str, profile_id: str):
    return SimpleNamespace(brand=brand, profile_id=profile_id)


@pytest.mark.parametrize(
    "brand, profile_id, expected",
    [
        ("ijai", "ijai.v17", "ijai"),
        ("xiaomi", "xiaomi.c101", "ijai"),
        ("xiaomi", "xiaomi.b106bk", "ijai"),
        ("xiaomi", "xiaomi.e101gb", "xiaomi"),
        ("xiaomi", "xiaomi.ov21gl", "xiaomi"),
        ("xiaomi", "xiaomi.ov31gl", "xiaomi"),
        ("xiaomi", "xiaomi.ov42gl", "xiaomi"),
        ("xiaomi", "xiaomi.ov43gb", "xiaomi"),
        ("xiaomi", "xiaomi.ov71gl", "xiaomi"),
        ("xiaomi", "xiaomi.ov81gl", "xiaomi"),
        ("dreame", "dreame.p2008", "dreame"),
        ("viomi", "viomi.v12", "viomi"),
        ("roidmi", "roidmi.s10", "roidmi"),
    ],
)
def test_parser_key(brand, profile_id, expected):
    assert map_parsers.parser_key(_profile(brand, profile_id)) == expected


def test_required_map_key_inputs_for_xiaomi_rebrand():
    from spec.registry import get_profile

    profile = get_profile("xiaomi.vacuum.c101")
    key = map_parsers.parser_key(profile)
    assert key == "ijai"
    assert map_parsers.required_map_key_inputs(key) == {"wifi_sn", "device_mac"}


@pytest.mark.parametrize(
    "model",
    ["dreame.vacuum.p2009", "dreame.vacuum.p2036", "dreame.vacuum.r2215"],
)
def test_dreame_phase1_hub_profiles_route_to_dreame_parser(model):
    from spec.registry import get_profile

    profile = get_profile(model)
    assert profile.map is not None
    assert map_parsers.parser_key(profile) == "dreame"

@pytest.mark.parametrize(
    "model",
    [
        "xiaomi.vacuum.d109gl",
        "xiaomi.vacuum.c107",
        "xiaomi.vacuum.d101",
        "xiaomi.vacuum.d102ev",
        "xiaomi.vacuum.d102gl",
        "xiaomi.vacuum.ov31gl",
    ],
)
def test_d109gl_family_routes_to_xiaomi_json_parser(model):
    from spec.registry import get_profile
    profile = get_profile(model)
    assert profile is not None
    assert map_parsers.parser_key(profile) == "xiaomi"

def test_has_ijai_grid_only_ijai():
    assert map_parsers.has_ijai_grid("ijai") is True
    for brand in ("xiaomi", "dreame", "viomi", "roidmi"):
        assert map_parsers.has_ijai_grid(brand) is False


@pytest.mark.parametrize(
    "brand, profile_id, model, expected_cls",
    [
        ("ijai", "ijai.v17", "ijai.vacuum.v17", "IjaiMapDataParser"),
        ("xiaomi", "xiaomi.ov21gl", "xiaomi.vacuum.ov21gl", "XiaomiMapDataParser"),
        ("dreame", "dreame.p2008", "dreame.vacuum.p2008", "DreameMapDataParser"),
        ("viomi", "viomi.v18", "viomi.vacuum.v18", "ViomiMapDataParser"),
        ("roidmi", "roidmi.r1b", "roidmi.vacuum.r1b", "RoidmiMapDataParser"),
    ],
)
def test_make_parser_picks_right_class(brand, profile_id, model, expected_cls):
    from vacuum_map_parser_base.config.color import ColorsPalette
    from vacuum_map_parser_base.config.image_config import ImageConfig
    from vacuum_map_parser_base.config.size import Sizes

    key = map_parsers.parser_key(_profile(brand, profile_id))
    parser = map_parsers.make_parser(
        key, model, ColorsPalette(), Sizes(), [], ImageConfig(), [])
    assert type(parser).__name__ == expected_cls


def test_make_parser_rejects_unknown_brand():
    with pytest.raises(ValueError):
        map_parsers.make_parser("roborock", "roborock.vacuum.a01",
                                None, None, [], None, [])


def test_required_map_key_inputs_ijai():
    """ijai requires wifi_sn and device_mac to derive the AES key."""
    keys = map_parsers.required_map_key_inputs("ijai")
    assert "wifi_sn" in keys
    assert "device_mac" in keys


@pytest.mark.parametrize("brand", ["xiaomi", "dreame", "viomi", "roidmi"])
def test_required_map_key_inputs_non_ijai_empty(brand):
    """Non-ijai brands need no local key material from the device."""
    assert map_parsers.required_map_key_inputs(brand) == frozenset()


def test_required_map_key_inputs_unknown_raises():
    with pytest.raises(ValueError):
        map_parsers.required_map_key_inputs("roborock")


@pytest.mark.parametrize(
    "key, expected",
    [
        ("ijai", "get_interim_file_url_pro"),
        ("xiaomi", "get_interim_file_url"),
        ("dreame", "get_interim_file_url"),
        ("viomi", "get_interim_file_url"),
        ("roidmi", "get_interim_file_url"),
    ],
)
def test_map_url_endpoint(key, expected):
    assert map_parsers.map_url_endpoint(key) == expected


def test_dreame_extract_enckey():
    assert map_parsers.dreame_extract_enckey("user/did/map,THEKEY") == "THEKEY"
    assert map_parsers.dreame_extract_enckey("no_comma") is None
    assert map_parsers.dreame_extract_enckey("") is None
    # split on first comma only; key is parts[1], not everything after first comma
    assert map_parsers.dreame_extract_enckey("path,KEY123") == "KEY123"


def test_dreame_decrypt_cloud_blob_roundtrip():
    """Encrypt with the Tasshack chain, verify dreame_decrypt_cloud_blob inverts it."""
    import base64
    import hashlib
    import zlib

    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    plaintext = b"synthetic dreame map data " * 4
    enckey = "test_enckey_xyz"
    compressed = zlib.compress(plaintext)
    key = hashlib.sha256(enckey.encode()).hexdigest()[:32].encode("utf8")
    encrypted = AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16).encrypt(pad(compressed, 16))
    raw = base64.b64encode(encrypted)

    result = map_parsers.dreame_decrypt_cloud_blob(raw, enckey)
    assert result == plaintext


def test_unpack_kwargs_per_brand():
    kw = dict(wifi_sn="SN", owner_id="OID", device_id="DID",
              model="m", device_mac="mac", enckey=None)
    assert map_parsers.unpack_kwargs("ijai", **kw) == {
        "wifi_sn": "SN", "owner_id": "OID", "device_id": "DID",
        "model": "m", "device_mac": "mac"}
    assert map_parsers.unpack_kwargs("xiaomi", **kw) == {"model": "m", "device_id": "DID"}
    # dreame: no enckey -> empty (parser falls back to plain zlib)
    assert map_parsers.unpack_kwargs("dreame", **kw) == {}
    assert map_parsers.unpack_kwargs("dreame", **{**kw, "enckey": "K"}) == {"enckey": "K"}
    assert map_parsers.unpack_kwargs("viomi", **kw) == {}
    assert map_parsers.unpack_kwargs("roidmi", **kw) == {}
