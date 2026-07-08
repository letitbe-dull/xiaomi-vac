"""Phase 3 synthetic Dreame map tests: mechanics only, no real blob.

Covers parser construction per model, the three decrypt paths (parser-IV,
zero-IV, plain), fetch() failure fallbacks, enckey extraction edges, and
the enckey re-poll after a decrypt failure. See dreame-map.md.
"""
from __future__ import annotations

import base64
import hashlib
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

    def cloud_get_prop(self, server, did, siid, piid):
        self.prop_calls += 1
        assert (siid, piid) == (6, 3)
        return {"result": [{"value": self.prop_value}]}

    def map_url(self, server, did, slot, endpoint):
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
