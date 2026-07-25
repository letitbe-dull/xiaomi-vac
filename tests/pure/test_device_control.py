"""Pure tests for IjaiVacuumDevice control calls."""
from __future__ import annotations

import pytest

from .helpers import FakeMiotDevice, load_device_module


def _last_calls():
    return FakeMiotDevice.instances[-1].calls


def test_start_stop_and_return_home_use_core_actions(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")

    assert (device.core.start.siid, device.core.start.aiid) == (2, 1)
    assert (device.core.stop.siid, device.core.stop.aiid) == (2, 2)
    assert (device.core.charge.siid, device.core.charge.aiid) == (3, 1)

    device.start()
    device.stop()
    device.return_home()

    assert _last_calls() == [
        ("action", 2, 1, []),
        ("action", 2, 2, []),
        ("action", 3, 1, []),
    ]


def test_pause_falls_back_to_stop_when_no_pause_action(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")

    assert device.core.pause is None

    device.pause()

    assert _last_calls() == [("action", device.core.stop.siid, device.core.stop.aiid, [])]


def test_pause_uses_real_pause_action_when_present(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "viomi.vacuum.v12")

    assert device.core.pause is not None

    device.pause()

    assert _last_calls() == [
        ("action", device.core.pause.siid, device.core.pause.aiid, [])
    ]


def test_set_fan_speed_uses_value_table(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")

    device.set_fan_speed("standard")

    assert _last_calls() == [
        (
            "set",
            device.core.fan_speed.siid,
            device.core.fan_speed.piid,
            device.core.fan_speeds["standard"],
        )
    ]


def test_set_fan_speed_rejects_unknown_label(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")

    with pytest.raises(KeyError):
        device.set_fan_speed("Turbo Plus")

    assert _last_calls() == []


def test_clean_segments_uses_v17_room_clean_action(monkeypatch):
    """set-room-clean (7/3) takes map room ids as a CSV STRING; the
    start-room-sweep action (2/7) wants Mijia ids and fails with map ids
    (verified on v17 hardware, issue #7)."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")

    device.clean_segments([10, 12])

    assert _last_calls() == [("action", 7, 3, ["10,12", 0, 1])]


def test_clean_segments_uses_v3_room_clean_action(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v3")

    device.clean_segments([10, 12])

    assert _last_calls() == [("action", 7, 3, ["10,12", 0, 1])]


def test_request_map_upload_prefers_upload_by_mapid_ii(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v3")

    device.request_map_upload(7)

    assert _last_calls() == [("action", 10, 14, [7])]


def test_request_map_upload_falls_back_to_upload_by_mapid(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v3")
    FakeMiotDevice.action_results[(10, 14)] = RuntimeError("-1")

    device.request_map_upload(7)

    assert _last_calls() == [
        ("action", 10, 14, [7]),
        ("action", 10, 2, [7]),
    ]


def test_map_list_parses_map_list_output(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    action = device.profile.map.get_map_list
    FakeMiotDevice.action_results = {
        (action.siid, action.aiid): {
            "out": [{"piid": 4, "value": '[{"name": "Home", "id": 1, "cur": 1}]'}]
        }
    }

    assert device.map_list() == [{"name": "Home", "id": 1, "cur": 1}]


def test_map_list_returns_empty_for_bad_json(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    action = device.profile.map.get_map_list
    FakeMiotDevice.action_results = {
        (action.siid, action.aiid): {"out": [{"piid": 4, "value": "not json"}]}
    }

    assert device.map_list() == []


def test_map_list_returns_empty_for_non_list_map_capability(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "dreame.vacuum.p2008")

    assert device.map_list() == []
    assert _last_calls() == []


def test_map_list_reads_viomi_out_piid_11(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "viomi.vacuum.v12")
    action = device.profile.map.get_map_list
    assert action.out_piids == (11,)
    FakeMiotDevice.action_results = {
        (action.siid, action.aiid): {
            "out": [{"piid": 11, "value": '[{"name": "Home", "id": 1, "cur": true}]'}]
        }
    }

    assert device.map_list() == [{"name": "Home", "id": 1, "cur": True}]


def test_map_list_rejects_viomi_v15_array_of_arrays(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "viomi.vacuum.v15")
    action = device.profile.map.get_map_list
    FakeMiotDevice.action_results = {
        (action.siid, action.aiid): {
            "out": [{"piid": 11, "value": '[["bkmap", "record", 1620954322, "Map 1", 1]]'}]
        }
    }

    assert device.map_list() == []


def test_map_list_rejects_non_dict_items(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    action = device.profile.map.get_map_list
    FakeMiotDevice.action_results = {
        (action.siid, action.aiid): {"out": [{"piid": 4, "value": '["Home", "Office"]'}]}
    }

    assert device.map_list() == []


def test_status_batches_reads_at_profile_max_properties_on_legacy_v3(monkeypatch):
    """v3 (IJAI_CORE_LEGACY) rejects large batches; profile caps the chunk at 5."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v3")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    device.status()

    assert device.profile.max_properties == 5
    assert FakeMiotDevice.instances[-1].batch_max_properties == [5]


def test_status_sends_unbatched_read_on_v17(monkeypatch):
    """v17 has no max_properties cap — the whole poll goes in one call."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    device.status()

    assert device.profile.max_properties is None
    assert FakeMiotDevice.instances[-1].batch_max_properties == [None]


def test_unsupported_property_and_action_raise_value_error(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "dreame.vacuum.p2008")

    with pytest.raises(ValueError):
        device.set_alarm(True)
