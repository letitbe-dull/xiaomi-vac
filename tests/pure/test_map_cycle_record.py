"""Pure-tier tests for the map fetch cycle diagnostic record.

The record is the tested artefact: what MapFetcher.fetch() leaves behind per
slot, how the active map id is resolved and by which trust step, and the shape
of the whole-cycle record. No homeassistant import; no network.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from PIL import Image

from xvac.map import MapFetcher, SessionExpired
from xvac.map_diagnostics import MapCycleRecord, SlotAttempt, describe_map_capability, resolve_active_id


class FakeCloud:
    """Duck-typed XiaomiCloud: canned URL and blob."""

    def __init__(self, *, url="http://x", blob=b""):
        self.url = url
        self.blob = blob
        self.calls = []

    def cloud_get_prop(self, server, did, siid, piid):
        return {"result": [{"value": None}]}

    def map_url(self, server, did, slot, endpoint):
        self.calls.append(("map_url", slot))
        return self.url

    def download(self, url):
        self.calls.append(("download", url))
        return self.blob


def _fetcher(cloud):
    return MapFetcher(
        cloud, server="de", user_id="1", device_id="2",
        model="dreame.vacuum.p2008", mac="AA", wifi_sn="SN", parser_brand="dreame")


def _md(*, image):
    return SimpleNamespace(
        image=image, calibration=lambda: [], rooms={}, charger=None,
        vacuum_position=None, vacuum_room=None, vacuum_room_name=None,
        zones=[], no_go_areas=[], no_mopping_areas=[], walls=[],
        path=None, goto=None,
    )


def _with_parser(fetcher, *, unpack, parse):
    fetcher._parser = SimpleNamespace(unpack_map=unpack, parse=parse)


def _renderable_md():
    return _md(image=SimpleNamespace(is_empty=False, data=Image.new("RGB", (8, 8), "white")))


# --- per-slot record from MapFetcher.fetch() -------------------------------
def test_fetch_records_no_url_and_still_raises_session_expired():
    f = _fetcher(FakeCloud(url=None))
    with pytest.raises(SessionExpired):
        f.fetch("0")
    attempt = f.last_attempt
    assert attempt.slot == "0"
    assert attempt.url_obtained is False
    assert attempt.blob_bytes is None
    assert attempt.outcome == "no_url"


def test_fetch_records_empty_download_as_none_return():
    f = _fetcher(FakeCloud(blob=b""))
    assert f.fetch("1") is None
    attempt = f.last_attempt
    assert attempt.slot == "1"
    assert attempt.url_obtained is True
    assert attempt.blob_bytes == 0
    assert attempt.outcome == "empty_download"


def test_fetch_records_undecryptable_blob():
    f = _fetcher(FakeCloud(blob=b"garbage-bytes"))

    def _boom(raw, **kw):
        raise ValueError("Padding is incorrect")

    _with_parser(f, unpack=_boom, parse=lambda u: None)
    assert f.fetch() is None
    attempt = f.last_attempt
    assert attempt.url_obtained is True
    assert attempt.blob_bytes == 13
    assert attempt.outcome == "undecryptable"


def test_fetch_records_blob_that_does_not_parse():
    f = _fetcher(FakeCloud(blob=b"x" * 40))

    def _bad_parse(unpacked):
        raise ValueError("bad frame")

    _with_parser(f, unpack=lambda raw, **kw: b"u", parse=_bad_parse)
    assert f.fetch() is None
    attempt = f.last_attempt
    assert attempt.blob_bytes == 40
    assert attempt.outcome == "parse_rejected"


def test_fetch_records_parse_with_no_image():
    f = _fetcher(FakeCloud(blob=b"x" * 40))
    _with_parser(f, unpack=lambda raw, **kw: b"u", parse=lambda u: _md(image=None))
    assert f.fetch() is None
    assert f.last_attempt.outcome == "empty_render"


def test_fetch_records_blob_that_parses_to_a_render():
    f = _fetcher(FakeCloud(blob=b"x" * 40))
    _with_parser(f, unpack=lambda raw, **kw: b"u", parse=lambda u: _renderable_md())
    result = f.fetch()
    assert result is not None
    attempt = f.last_attempt
    assert attempt.url_obtained is True
    assert attempt.blob_bytes == 40
    assert attempt.outcome == "rendered"


def test_fetch_record_never_contains_the_url_value():
    f = _fetcher(FakeCloud(url="https://signed.example/secret-token-abc", blob=b""))
    f.fetch()
    assert "secret-token-abc" not in json.dumps(f.last_attempt.as_dict())


# --- active-map resolution: which ADR-0013 trust step decided ---------------
_SINGLE = 0


def _resolve(*, decoded=(), active_meta=None, mqtt=None, has_map_list=True, maps_meta=()):
    return resolve_active_id(
        [SimpleNamespace(map_id=i) for i in decoded], active_meta, mqtt,
        has_map_list, list(maps_meta), single_map_id=_SINGLE)


def test_resolution_prefers_the_live_blobs_embedded_id():
    assert _resolve(decoded=[555], active_meta={"id": 111, "cur": True}) == (555, "blob")


def test_resolution_uses_map_list_cur_when_blob_carries_no_id():
    assert _resolve(decoded=[None], active_meta={"id": "111", "cur": True}) == (111, "map_list")


def test_resolution_uses_single_map_key_only_when_profile_has_no_map_list():
    assert _resolve(has_map_list=False, maps_meta=[]) == (_SINGLE, "single_map")


def test_empty_map_list_read_on_multi_map_device_is_unresolved():
    assert _resolve(has_map_list=True, maps_meta=[]) == (None, None)


def test_resolution_falls_to_mqtt_id_when_map_list_read_failed():
    assert _resolve(mqtt=777) == (777, "mqtt")


def test_unparseable_map_list_id_is_skipped():
    assert _resolve(active_meta={"id": "abc", "cur": True}, mqtt=9) == (9, "mqtt")


# --- whole-cycle record ------------------------------------------------------
def _record(**kw):
    return MapCycleRecord(parser_key="ijai", map_capability=None, **kw)


def _slots(*outcomes):
    """(url_obtained, blob_bytes, outcome) per slot."""
    return [
        SlotAttempt(slot=str(i), url_obtained=u, blob_bytes=b, outcome=o)
        for i, (u, b, o) in enumerate(outcomes)
    ]


def test_cycle_where_cloud_refused_every_url_reads_as_session_expired():
    rec = _record(slots=_slots((False, None, "no_url"), (False, None, "no_url")))
    out = rec.as_dict()
    assert out["session_expired"] is True
    assert out["url_obtained"] is False
    assert out["rendered"] is False


def test_cycle_with_url_but_empty_blob_is_not_session_expired():
    rec = _record(slots=_slots((True, 0, "empty_download"), (True, 0, "empty_download")))
    out = rec.as_dict()
    assert out["session_expired"] is False
    assert out["url_obtained"] is True
    assert out["rendered"] is False


def test_cycle_with_blob_that_does_not_parse_is_not_rendered():
    rec = _record(slots=_slots((True, 52000, "parse_rejected"), (True, 0, "empty_download")))
    out = rec.as_dict()
    assert out["rendered"] is False
    assert out["slots"][0]["blob_bytes"] == 52000


def test_cycle_with_one_readable_slot_is_rendered_even_if_the_other_is_dead():
    rec = _record(slots=_slots((False, None, "no_url"), (True, 52000, "rendered")))
    out = rec.as_dict()
    assert out["rendered"] is True
    assert out["session_expired"] is False


def test_cycle_before_any_fetch_is_not_reported_as_session_expired():
    assert _record().as_dict()["session_expired"] is False


def test_cycle_served_is_cache_when_nothing_decoded_but_cache_held_the_map():
    rec = _record()
    rec.set_served(decoded=False, have_result=True)
    assert rec.as_dict()["served"] == "cache"


def test_cycle_served_is_live_when_a_slot_decoded():
    rec = _record()
    rec.set_served(decoded=True, have_result=True)
    assert rec.as_dict()["served"] == "live"


def test_cycle_served_is_none_on_cold_start():
    rec = _record()
    rec.set_served(decoded=False, have_result=False)
    assert rec.as_dict()["served"] == "none"


def test_cycle_record_is_json_serialisable_and_carries_resolution():
    rec = MapCycleRecord(
        parser_key="xiaomi", map_capability={"service": 10, "declared": {}},
        slots=_slots((True, 10, "rendered")), resolved_map_id=42, resolved_by="blob")
    out = json.loads(json.dumps(rec.as_dict()))
    assert out["parser_key"] == "xiaomi"
    assert out["resolved_map_id"] == 42
    assert out["resolved_by"] == "blob"


# --- profile map capability shape --------------------------------------------
def test_capability_shape_lists_only_declared_ids():
    from spec.types import Action, MapCapability, Prop

    cap = MapCapability(service=10, map_num=Prop(10, 4), get_map_list=Action(10, 3))
    assert describe_map_capability(cap) == {
        "service": 10, "declared": {"map_num": "10.4", "get_map_list": "10.3"}}


def test_capability_shape_is_none_for_profile_without_map():
    assert describe_map_capability(None) is None


def test_capability_shape_of_every_registered_profile_is_serialisable():
    from spec.registry import MODEL_PROFILES

    for profile in MODEL_PROFILES.values():
        json.dumps(describe_map_capability(profile.map))
