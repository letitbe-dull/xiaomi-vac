"""Pure-tier tests for per-brand map decode.

No homeassistant import — runs on native Windows. Covers:
  - the ijai labelled-grid vector contract (`map_vector.extract_grid` /
    `vector_map`), the part that's brand-specific;
  - the brand dispatch (`map_parsers`) that picks the right parser + unpack
    inputs + grid flag per brand.

The repo's `current_map.bin` is an AES-encrypted ijai blob whose key inputs
(wifi_sn/owner_id/device_id/mac) are device secrets not stored here, so it can't
be decrypted in a unit test. Instead we build a synthetic ijai `RobotMap`
protobuf — the exact shape `extract_grid` consumes — so the contract is exercised
end to end without secrets. Per-brand fixtures get added as real blobs appear.
"""
from __future__ import annotations

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


def test_vector_map_non_ijai_empty_grid():
    """Non-ijai brands carry overlays but no labelled grid (best-effort)."""
    out = map_vector.vector_map(_fake_md(), b"", ijai_grid=False)

    assert out["grid_rle"] == []
    assert out["room_chains"] == []
    assert out["map_id"] is None
    # overlays still come through so the card can draw on the PNG
    assert out["charger"] == {"x": 0.5, "y": -0.5}
    assert out["rooms"][0]["name"] == "Kitchen"


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
