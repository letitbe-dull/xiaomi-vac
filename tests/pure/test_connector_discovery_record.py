"""Pure tests for the per-region discovery record left by XiaomiCloud.list_vacuums()."""
from __future__ import annotations

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


def test_all_regions_answer_with_vacuums():
    cloud = _cloud()
    resp = _answer([_device("1"), _device("2", "roborock.vacuum.s5"), _device("3", "yeelink.light.x")])
    with patch.object(cloud, "_call", return_value=resp):
        found = cloud.list_vacuums()
    assert [d["did"] for d in found] == ["1", "2"]
    assert [r["region"] for r in cloud.discovery_record] == SERVERS
    assert all(r == {"region": r["region"], "answered": True, "devices": 3, "vacuums": 2}
               for r in cloud.discovery_record)


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
               for r in cloud.discovery_record)


def test_some_regions_answer_and_some_do_not():
    cloud = _cloud()
    seen_urls: list[str] = []

    def fake(url: str, params: dict):
        seen_urls.append(url)
        if len(seen_urls) == 2:  # second region ("de") answers with one vacuum
            return _answer([_device("1"), _device("2", "yeelink.light.x")])
        if len(seen_urls) == 3:  # third region answers empty
            return _answer([])
        return None

    with patch.object(cloud, "_call", side_effect=fake):
        found = cloud.list_vacuums()
    assert [d["did"] for d in found] == ["1"]
    assert found[0]["server"] == SERVERS[1]
    record = _record(cloud)
    assert record[SERVERS[0]]["answered"] is False
    assert (record[SERVERS[1]]["answered"], record[SERVERS[1]]["devices"], record[SERVERS[1]]["vacuums"]) == (True, 2, 1)
    assert (record[SERVERS[2]]["answered"], record[SERVERS[2]]["devices"], record[SERVERS[2]]["vacuums"]) == (True, 0, 0)
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
    assert set(cloud.discovery_record[0]) == {"region", "answered", "devices", "vacuums"}


def test_discovery_logs_vacuum_model_without_identifiers(caplog):
    cloud = _cloud()
    dev = _device("1", "ijai.vacuum.v17x") | {"token": "SECRETTOKEN", "mac": "AA:BB", "localip": "10.0.0.5"}
    with caplog.at_level("DEBUG"), patch.object(cloud, "_call", return_value=_answer([dev])):
        cloud.list_vacuums()
    assert "Discovery region=cn vacuum model=ijai.vacuum.v17x" in caplog.text
    for secret in ("SECRETTOKEN", "AA:BB", "10.0.0.5"):
        assert secret not in caplog.text
