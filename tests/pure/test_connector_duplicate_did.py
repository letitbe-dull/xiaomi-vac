"""Pure tests for which regional record list_vacuums() keeps when a did repeats."""
from __future__ import annotations

from unittest.mock import patch

from cloud.connector import XiaomiCloud

_MISSING = object()


def _cloud() -> XiaomiCloud:
    cloud = XiaomiCloud("user@example.com")
    cloud.user_id = "9876543210"
    return cloud


def _record(region: str, online=_MISSING, did: str = "1", model: str = "ijai.vacuum.v3") -> dict:
    rec = {
        "name": f"vac-{region}", "did": did, "model": model, "mac": f"mac-{region}",
        "localip": f"ip-{region}", "token": f"token-{region}",
    }
    if online is not _MISSING:
        rec["isOnline"] = online
    return rec


def _list(regions: dict[str, list[dict]]) -> list[dict]:
    """Run list_vacuums() with each region answering its own canned device list."""
    cloud = _cloud()

    by_url = {cloud._api_url(r) + "/home/device_list": devices for r, devices in regions.items()}

    def fake(url: str, params: dict):
        if url not in by_url:
            return None
        return {"code": 0, "result": {"list": by_url[url]}}

    with patch.object(cloud, "_call", side_effect=fake):
        return cloud.list_vacuums()


def test_online_region_wins_over_earlier_offline_region_whole_record():
    found = _list({"de": [_record("de", False)], "ru": [_record("ru", True)]})
    assert found == [{
        "name": "vac-ru", "did": "1", "model": "ijai.vacuum.v3", "mac": "mac-ru",
        "localip": "ip-ru", "token": "token-ru", "server": "ru", "owner_uid": "",
    }]


def test_both_offline_first_region_wins_whole_record():
    found = _list({"de": [_record("de", False)], "ru": [_record("ru", False)]})
    assert len(found) == 1
    assert found[0]["server"] == "de"
    assert (found[0]["mac"], found[0]["localip"], found[0]["token"]) == ("mac-de", "ip-de", "token-de")


def test_both_online_first_online_region_wins():
    found = _list({"de": [_record("de", True)], "ru": [_record("ru", True)]})
    assert len(found) == 1
    assert found[0]["server"] == "de"
    assert found[0]["token"] == "token-de"


def test_first_online_wins_among_three_regions():
    found = _list({
        "cn": [_record("cn", False)], "de": [_record("de", True)], "ru": [_record("ru", True)],
    })
    assert [d["server"] for d in found] == ["de"]


def test_missing_is_online_counts_as_offline():
    found = _list({"de": [_record("de")], "ru": [_record("ru", True)]})
    assert [d["server"] for d in found] == ["ru"]


def test_non_boolean_is_online_counts_as_offline():
    for value in ("true", 1, None, "yes"):
        found = _list({"de": [_record("de", False)], "ru": [_record("ru", value)]})
        assert [d["server"] for d in found] == ["de"], value


def test_non_boolean_earlier_record_does_not_block_online_later_record():
    found = _list({"de": [_record("de", 1)], "ru": [_record("ru", True)]})
    assert [d["server"] for d in found] == ["ru"]


def test_single_region_offline_device_is_bound_as_before():
    found = _list({"ru": [_record("ru", False)]})
    assert found == [{
        "name": "vac-ru", "did": "1", "model": "ijai.vacuum.v3", "mac": "mac-ru",
        "localip": "ip-ru", "token": "token-ru", "server": "ru", "owner_uid": "",
    }]


def test_single_region_device_without_is_online_is_bound_as_before():
    found = _list({"sg": [_record("sg")]})
    assert [(d["did"], d["server"], d["token"]) for d in found] == [("1", "sg", "token-sg")]


def test_no_duplicate_dids_are_returned():
    found = _list({
        "de": [_record("de", False), _record("de", True, did="2")],
        "ru": [_record("ru", True), _record("ru", False, did="2"), _record("ru", True, did="3")],
        "sg": [_record("sg", True)],
    })
    dids = [d["did"] for d in found]
    assert sorted(dids) == ["1", "2", "3"]
    assert len(dids) == len(set(dids))
    by_did = {d["did"]: d["server"] for d in found}
    assert by_did == {"1": "ru", "2": "de", "3": "ru"}


def test_non_vacuum_with_repeated_did_is_ignored():
    found = _list({"de": [_record("de", False, model="yeelink.light.x")],
                   "ru": [_record("ru", True, model="yeelink.light.x")]})
    assert found == []
