"""Pure tests for the per-region discovery record left by XiaomiCloud.list_vacuums()."""
from __future__ import annotations

import json
from unittest.mock import patch

from cloud.connector import SERVERS, XiaomiCloud


def _cloud() -> XiaomiCloud:
    cloud = XiaomiCloud("user@example.com")
    cloud.user_id = "9876543210"
    return cloud


def _device(did: str, model: str = "xiaomi.vacuum.d106gl") -> dict:
    return {"name": f"n{did}", "did": did, "model": model, "mac": "", "localip": "", "token": ""}


def _answer(devices: list[dict]) -> dict:
    return {"code": 0, "result": {"list": devices}}


def _record(cloud: XiaomiCloud) -> dict:
    return {r["region"]: r for r in cloud.discovery_record}


def _discover(cloud: XiaomiCloud, flat: dict, gethome: dict | None = None,
              home_devices: dict | None = None) -> list[dict]:
    """Run list_vacuums() against a fake cloud routed by URL; anything unlisted doesn't answer.

    @param flat: region -> flat device list
    @param gethome: region -> gethome result body
    @param home_devices: (home_id, home_owner) -> that home's device list
    @returns the discovered vacuums
    """
    gethome = gethome or {}
    home_devices = home_devices or {}

    def fake(url: str, params: dict):
        for region in SERVERS:
            base = cloud._api_url(region)
            if url == base + "/home/device_list" and region in flat:
                return _answer(flat[region])
            if url == base + "/v2/homeroom/gethome" and region in gethome:
                return {"code": 0, "result": gethome[region]}
        if url.endswith("/v2/home/home_device_list"):
            body = json.loads(params["data"])
            devices = home_devices.get((body["home_id"], body["home_owner"]))
            if devices is not None:
                return {"code": 0, "result": {"device_info": devices}}
        return None

    with patch.object(cloud, "_call", side_effect=fake):
        return cloud.list_vacuums()


def test_vacuum_only_in_a_shared_home_is_found_with_the_home_owners_uid():
    cloud = _cloud()
    found = _discover(
        cloud,
        flat={"de": []},
        gethome={"de": {"homelist": [], "share_home_list": [{"id": 500, "uid": 4444333322}]}},
        home_devices={(500, 4444333322): [_device("1") | {"uid": 1111111111}]},
    )
    assert [(d["did"], d["server"], d["owner_uid"]) for d in found] == [("1", "de", "4444333322")]


def test_vacuum_in_flat_list_and_a_home_is_returned_once_with_the_flat_record():
    cloud = _cloud()
    found = _discover(
        cloud,
        flat={"de": [_device("1") | {"token": "flat-token", "uid": 1111111111, "isOnline": False}]},
        gethome={"de": {"homelist": [{"id": 10, "uid": 2222222222}], "share_home_list": []}},
        home_devices={(10, 2222222222): [_device("1") | {"token": "home-token", "isOnline": True}]},
    )
    assert [(d["did"], d["token"], d["owner_uid"]) for d in found] == [("1", "flat-token", "1111111111")]


def test_failed_home_calls_keep_flat_vacuums_and_other_homes():
    cloud = _cloud()
    found = _discover(
        cloud,
        flat={"de": [_device("1")], "ru": []},
        gethome={"ru": {"homelist": [{"id": 20, "uid": 3333333333}],
                        "share_home_list": [{"id": 21, "uid": 4444333322}]}},
        home_devices={(21, 4444333322): [_device("2")]},
    )
    assert [(d["did"], d["server"]) for d in found] == [("1", "de"), ("2", "ru")]


def test_all_regions_answer_with_vacuums():
    cloud = _cloud()
    resp = _answer([_device("1"), _device("2", "roborock.vacuum.s5"), _device("3", "yeelink.light.x")])
    with patch.object(cloud, "_call", return_value=resp):
        found = cloud.list_vacuums()
    assert [d["did"] for d in found] == ["1", "2"]
    assert [r["region"] for r in cloud.discovery_record] == SERVERS
    assert all(r == {"region": r["region"], "answered": True, "devices": 3, "vacuums": 2,
                     "homes_answered": True, "owned_homes": 0, "shared_homes": 0,
                     "home_only_vacuums": 0}
               for r in cloud.discovery_record)


def test_list_vacuums_returns_each_devices_owner_uid():
    cloud = _cloud()
    resp = _answer([_device("1") | {"uid": 1111111111}, _device("2") | {"uid": "2222222222"}])
    with patch.object(cloud, "_call", return_value=resp):
        found = cloud.list_vacuums()
    assert [(d["did"], d["owner_uid"]) for d in found] == [("1", "1111111111"), ("2", "2222222222")]


def test_all_regions_answer_with_zero_devices():
    cloud = _cloud()
    with patch.object(cloud, "_call", return_value=_answer([])):
        found = cloud.list_vacuums()
    assert found == []
    assert all(r["answered"] and r["devices"] == 0 and r["vacuums"] == 0
               for r in cloud.discovery_record)
    assert len(cloud.discovery_record) == len(SERVERS)


def test_no_region_answers():
    cloud = _cloud()
    with patch.object(cloud, "_call", return_value=None):
        found = cloud.list_vacuums()
    assert found == []
    assert [r["region"] for r in cloud.discovery_record] == SERVERS
    assert all(r["answered"] is False and r["devices"] == 0 and r["vacuums"] == 0
               and r["homes_answered"] is False
               for r in cloud.discovery_record)


def test_some_regions_answer_and_some_do_not():
    cloud = _cloud()
    found = _discover(
        cloud,
        flat={SERVERS[1]: [_device("1"), _device("2", "yeelink.light.x")], SERVERS[2]: []},
        gethome={SERVERS[1]: {"homelist": [{"id": 1, "uid": 9876543210}],
                              "share_home_list": [{"id": 2, "uid": 4444333322}]}},
        home_devices={(1, 9876543210): [_device("1")], (2, 4444333322): []},
    )
    assert [d["did"] for d in found] == ["1"]
    assert found[0]["server"] == SERVERS[1]
    record = _record(cloud)
    assert record[SERVERS[0]]["answered"] is False
    assert record[SERVERS[1]] == {
        "region": SERVERS[1], "answered": True, "devices": 2, "vacuums": 1,
        "homes_answered": True, "owned_homes": 1, "shared_homes": 1, "home_only_vacuums": 0,
    }
    assert record[SERVERS[2]] == {
        "region": SERVERS[2], "answered": True, "devices": 0, "vacuums": 0,
        "homes_answered": False, "owned_homes": 0, "shared_homes": 0, "home_only_vacuums": 0,
    }
    assert all(record[s]["answered"] is False for s in SERVERS[3:])


def test_record_is_replaced_on_each_attempt():
    cloud = _cloud()
    with patch.object(cloud, "_call", return_value=None):
        cloud.list_vacuums()
    with patch.object(cloud, "_call", return_value=_answer([_device("1")])):
        cloud.list_vacuums()
    assert len(cloud.discovery_record) == len(SERVERS)
    assert all(r["answered"] for r in cloud.discovery_record)


def test_record_holds_only_counts_and_region():
    cloud = _cloud()
    dev = _device("1") | {"token": "SECRETTOKEN", "mac": "AA:BB", "localip": "10.0.0.5"}
    with patch.object(cloud, "_call", return_value=_answer([dev])):
        cloud.list_vacuums()
    assert set(cloud.discovery_record[0]) == {
        "region", "answered", "devices", "vacuums",
        "homes_answered", "owned_homes", "shared_homes", "home_only_vacuums",
    }


def test_discovery_logs_vacuum_model_without_identifiers(caplog):
    cloud = _cloud()
    dev = _device("1", "ijai.vacuum.v17x") | {"token": "SECRETTOKEN", "mac": "AA:BB", "localip": "10.0.0.5"}
    with caplog.at_level("DEBUG"), patch.object(cloud, "_call", return_value=_answer([dev])):
        cloud.list_vacuums()
    assert "Discovery region=cn vacuum model=ijai.vacuum.v17x" in caplog.text
    for secret in ("SECRETTOKEN", "AA:BB", "10.0.0.5"):
        assert secret not in caplog.text
