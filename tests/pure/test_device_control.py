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

    device.set_fan_speed("Standard")

    assert _last_calls() == [
        (
            "set",
            device.core.fan_speed.siid,
            device.core.fan_speed.piid,
            device.core.fan_speeds["Standard"],
        )
    ]


def test_set_fan_speed_rejects_unknown_label(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")

    with pytest.raises(KeyError):
        device.set_fan_speed("Turbo Plus")

    assert _last_calls() == []


def test_clean_segments_uses_v17_room_clean_action(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")

    assert (device.profile.room_clean.start.siid, device.profile.room_clean.start.aiid) == (
        2,
        7,
    )
    assert device.profile.room_clean.start.in_piid == 10

    device.clean_segments([10, 12])

    assert _last_calls() == [("action", 2, 7, ["10,12"])]


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


def test_unsupported_property_and_action_raise_value_error(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "dreame.vacuum.p2008")

    with pytest.raises(ValueError):
        device.set_alarm(True)
