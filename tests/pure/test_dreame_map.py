"""Phase 3 synthetic Dreame map tests: mechanics only, no real blob.

Covers parser construction per model, the three decrypt paths (parser-IV,
zero-IV, plain), fetch() failure fallbacks, enckey extraction edges, and
the enckey re-poll after a decrypt failure. See .plans/dreame-map.md.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zlib
from types import SimpleNamespace

import pytest

import map_parsers
from spec.registry import MODEL_PROFILES

# Loaded via the synthetic `xvac` package (see conftest) — map.py has relative
# imports so it can't be imported standalone.
from xvac.map import MapFetcher, SessionExpired


def _dreame_models():
    return sorted(
        m for m, p in MODEL_PROFILES.items()
        if p.brand == "dreame" and p.map is not None
    )


# --- parser construction for every registered dreame model ---------------
@pytest.mark.parametrize("model", _dreame_models())
def test_dreame_parser_constructs(model):
    from vacuum_map_parser_base.config.color import ColorsPalette
    from vacuum_map_parser_base.config.image_config import ImageConfig
    from vacuum_map_parser_base.config.size import Sizes

    parser = map_parsers.make_parser(
        "dreame", model, ColorsPalette(), Sizes(), [], ImageConfig(), [])
    assert type(parser).__name__ == "DreameMapDataParser"


# --- enckey extraction edges ----------------------------------------------
def test_dreame_extract_enckey_edges():
    f = map_parsers.dreame_extract_enckey
    assert f("no_comma") is None          # no comma
    assert f("") is None                  # empty value
    assert f("path,KEY") == "KEY"         # one comma
    assert f("path,KEY,extra") == "KEY"   # extra comma data: key is parts[1]
    assert f("path,") is None             # empty key


# --- MapFetcher helpers ----------------------------------------------------
class FakeCloud:
    """Duck-typed XiaomiCloud: canned enckey prop, URL and blob."""

    def __init__(self, *, prop_value=None, url="http://x", blob=b""):
        self.prop_value = prop_value
        self.url = url
        self.blob = blob
        self.prop_calls = 0
        self.minted_object = None

    def get_user_device_data(self, server, did, siid, piid, limit=2, time_start=0):
        return None

    def cloud_get_prop(self, server, did, siid, piid):
        self.prop_calls += 1
        assert (siid, piid) == (6, 3)
        return {"result": [{"value": self.prop_value}]}

    def map_url(self, server, did, slot, endpoint, obj_name=None):
        self.minted_object = obj_name or f"1/{did}/{slot}"
        return self.url

    def download(self, url):
        return self.blob


def _fetcher(cloud, model="dreame.vacuum.p2008"):
    return MapFetcher(
        cloud, server="de", user_id="1", device_id="2", model=model,
        mac="AA", wifi_sn="SN", parser_brand="dreame")


def _zero_iv_blob(plaintext: bytes, enckey: str) -> bytes:
    """Encrypt with the Tasshack chain (inverse of dreame_decrypt_cloud_blob)."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    key = hashlib.sha256(enckey.encode()).hexdigest()[:32].encode("utf8")
    enc = AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16).encrypt(pad(zlib.compress(plaintext), 16))
    return base64.b64encode(enc)


class RecorderParser:
    """Stub parser recording unpack_map calls."""

    def __init__(self):
        self.calls = []

    def unpack_map(self, raw, **kw):
        self.calls.append((raw, kw))
        return b"unpacked"


# --- decrypt path selection -----------------------------------------------
def test_iv_model_delegates_to_parser_unpack(monkeypatch):
    from vacuum_map_parser_dreame.map_data_parser import DreameMapDataParser

    cloud = FakeCloud()
    f = _fetcher(cloud, model="dreame.vacuum.ivtest")
    f._enckey = "KEY"
    rec = RecorderParser()
    f._parser = rec
    monkeypatch.setitem(DreameMapDataParser.IVs, "dreame.vacuum.ivtest", "0102030405060708")

    assert f._unpack(b"raw") == b"unpacked"
    assert rec.calls == [(b"raw", {"enckey": "KEY"})]


def test_non_iv_model_uses_zero_iv_helper():
    from vacuum_map_parser_dreame.map_data_parser import DreameMapDataParser

    model = "dreame.vacuum.p2008"
    assert DreameMapDataParser.IVs.get(model) is None  # guard the premise

    cloud = FakeCloud()
    f = _fetcher(cloud, model=model)
    f._enckey = "KEY"
    rec = RecorderParser()
    f._parser = rec  # must NOT be called

    plaintext = b"synthetic map bytes " * 3
    assert f._unpack(_zero_iv_blob(plaintext, "KEY")) == plaintext
    assert rec.calls == []


def test_plain_path_no_enckey():
    """No enckey -> parser.unpack_map with no kwargs (plain zlib models)."""
    cloud = FakeCloud()
    f = _fetcher(cloud)
    assert f._enckey is None
    rec = RecorderParser()
    f._parser = rec

    f._unpack(b"raw")
    assert rec.calls == [(b"raw", {})]


# --- fetch() failure paths return None, never raise ------------------------
def test_fetch_no_url_raises_session_expired():
    f = _fetcher(FakeCloud(prop_value=None, url=None))
    with pytest.raises(SessionExpired):
        f.fetch()


def test_fetch_empty_download_returns_none():
    assert _fetcher(FakeCloud(blob=b"")).fetch() is None


@pytest.mark.parametrize("blob", [
    b"\xff\xfenot base64 or zlib",      # garbage
    b"AAAA",                             # valid b64, not zlib
    zlib.compress(b"not a map frame"),   # valid zlib, wrong frame
])
def test_fetch_bad_blob_returns_none(blob):
    assert _fetcher(FakeCloud(prop_value="path/obj", blob=blob)).fetch() is None


def test_fetch_empty_map_returns_none():
    """A parse that yields no image is reported as None, not a crash."""
    f = _fetcher(FakeCloud(blob=b"x"))
    f._parser = SimpleNamespace(
        unpack_map=lambda raw, **kw: b"u",
        parse=lambda unpacked: SimpleNamespace(image=None),
    )
    assert f.fetch() is None


def test_parse_failure_keeps_enckey(monkeypatch):
    """Decrypt OK but parser rejects the frame: key material is good, keep it."""
    from vacuum_map_parser_dreame.map_data_parser import DreameMapDataParser

    model = "dreame.vacuum.ivtest"
    monkeypatch.setitem(DreameMapDataParser.IVs, model, "0102030405060708")
    cloud = FakeCloud(prop_value="path/obj,KEY", blob=b"x")
    f = _fetcher(cloud, model=model)

    def _bad_parse(unpacked):
        raise ValueError("bad frame")

    # IV model -> _unpack delegates to the (stub) parser; decrypt "succeeds",
    # parse fails.
    f._parser = SimpleNamespace(unpack_map=lambda raw, **kw: b"u", parse=_bad_parse)

    assert f.fetch() is None
    assert f._enckey == "KEY"           # key kept
    assert f._enckey_polled is True     # no re-poll scheduled
    assert cloud.prop_calls == 1        # only the initial poll


# --- enckey re-poll after decrypt failure ----------------------------------
def test_fetch_repolls_enckey_after_decrypt_failure():
    cloud = FakeCloud(prop_value="path/obj,KEY", blob=b"garbage")
    f = _fetcher(cloud)

    assert f.fetch() is None            # decrypt fails
    assert f._enckey is None            # key dropped
    assert f._enckey_polled is False    # will re-poll
    assert f.fetch() is None
    assert cloud.prop_calls == 2        # polled again on the second fetch


# --- MH1: map URL is minted for the object the robot reports ----------------
@pytest.mark.parametrize("prop_value, minted", [
    ("1/2/device_reported_obj,KEY", "1/2/device_reported_obj"),  # path reported
    (",KEY", "1/2/0"),                                            # no path: guessed user_id/did/slot
])
def test_dreame_map_url_minted_for_reported_object(prop_value, minted):
    cloud = FakeCloud(prop_value=prop_value, blob=b"")
    _fetcher(cloud).fetch()
    assert cloud.minted_object == minted


# --- r2211o stale-room filter / outlines / map id ---------------------------
def _dreame_frame(width, height, cells, trailer, *, pixel_size=50, left=0, top=0):
    """Dreame I-frame: 27-byte header, width*height labelled cells, JSON trailer."""
    header = bytearray(27)
    header[17:19] = pixel_size.to_bytes(2, "little", signed=True)
    header[19:21] = width.to_bytes(2, "little", signed=True)
    header[21:23] = height.to_bytes(2, "little", signed=True)
    header[23:25] = left.to_bytes(2, "little", signed=True)
    header[25:27] = top.to_bytes(2, "little", signed=True)
    return bytes(header) + bytes(cells) + json.dumps(trailer).encode()


@pytest.mark.parametrize("cleanset", [
    {"1": [], "2": []},
    json.dumps({"1": [], "2": []}),   # r2211o nests the cleanset as a JSON string
])
def test_r2211o_room_list_drops_rooms_missing_from_cleanset(cleanset):
    f = _fetcher(FakeCloud(), model="dreame.vacuum.r2211o")
    frame = _dreame_frame(2, 2, [4, 8, 4, 8], {"cleanset": cleanset})
    md = SimpleNamespace(rooms={1: "kitchen", 2: "hall", 66: "stale"})

    f._filter_dreame_stale_rooms(md, frame)

    assert set(md.rooms) == {1, 2}


def test_dreame_outlines_one_per_labelled_room_and_overlays_in_metres():
    import map_parsers
    import map_vector

    # 6x2 grid: segment 1 (cell 4) on cols 0-2, segment 2 (cell 8) on cols 3-4,
    # segment 3 (cell 12) on col 5 is not a listed room, so gets no outline.
    # Room 5 is listed but has no cells.
    cells = [4, 4, 4, 8, 8, 12] * 2
    frame = _dreame_frame(6, 2, cells, {}, pixel_size=50, left=-100, top=0)
    md = SimpleNamespace(
        path=None, charger=SimpleNamespace(x=2000, y=-1000), vacuum_position=None,
        goto=None, walls=[], no_go_areas=[], no_mopping_areas=[], zones=[],
        vacuum_room=None, vacuum_room_name=None,
        rooms={
            1: SimpleNamespace(name="a", pos_x=None, pos_y=None, x0=0, y0=0, x1=3000, y1=1000),
            2: SimpleNamespace(name="b", pos_x=None, pos_y=None, x0=0, y0=0, x1=1000, y1=1000),
            5: SimpleNamespace(name="c", pos_x=None, pos_y=None, x0=0, y0=0, x1=1000, y1=1000),
        },
    )

    out = map_vector.vector_map(
        md, frame, ijai_grid=False, dreame_grid=True,
        units_per_metre=map_parsers.overlay_units_per_metre("dreame"),
    )

    assert sorted(c["id"] for c in out["room_chains"]) == [1, 2]
    assert out["bounds"] == pytest.approx(
        {"minX": -0.1, "minY": 0.0, "maxX": 0.2, "maxY": 0.1})
    assert out["charger"] == {"x": 2.0, "y": -1.0}
    assert out["rooms"][0]["bbox"] == [0.0, 0.0, 3.0, 1.0]


def test_mqtt_cur_map_id_ignored_without_map_list():
    from map_diagnostics import resolve_active_id

    for mqtt_id in (46, 45):
        assert resolve_active_id(
            [], None, mqtt_id, False, [], single_map_id=0,
        ) == (0, "single_map")
