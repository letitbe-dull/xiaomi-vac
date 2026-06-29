"""Pure tests for IjaiVacuumDevice status handling."""
from __future__ import annotations

import pytest

from .helpers import FakeMiotDevice, load_device_module


@pytest.mark.parametrize(
    ("raw_status", "expected_raw", "expected_activity"),
    [
        (5, 5, "cleaning"),
        (2, 2, "paused"),
        (999, 999, "idle"),
    ],
)
def test_status_maps_raw_activity(monkeypatch, raw_status, expected_raw, expected_activity):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): raw_status}

    status = device.status()

    assert status.raw_status == expected_raw
    assert status.activity == expected_activity


def test_status_raises_when_required_prop_fails(monkeypatch):
    """A failing status read must raise DeviceCommunicationError, not return idle."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {
        (status_prop.siid, status_prop.piid): RuntimeError("network timeout")
    }

    with pytest.raises(device_mod.DeviceCommunicationError):
        device.status()


def test_status_tolerates_optional_prop_none(monkeypatch):
    """Optional props returning None must not raise; required status must succeed."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    status_prop = device.core.status
    # Only supply the required status prop; everything else defaults to None via FakeMiotDevice.
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    status = device.status()

    assert status.raw_status == 5
    # Optional fields that have no prop on this model or no value stay None.
    assert status.sweep_type_raw is None or isinstance(status.sweep_type_raw, int)


def test_status_skips_absent_core_props(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "dreame.vacuum.p2008")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    assert device.core.sweep_type is None
    assert device.core.alarm is None

    status = device.status()

    assert status.sweep_type_raw is None
    assert status.alarm_raw is None
    assert all(call[1] is not None and call[2] is not None for call in FakeMiotDevice.instances[-1].calls)


def test_lean_core_fields_stay_parked(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    status = device.status()

    assert status.main_brush_life is None
    assert status.side_brush_life is None
    assert status.filter_life is None
    assert status.mop_life is None
    assert status.clean_area is None
    assert status.clean_time is None


def test_as_int_coercion(monkeypatch):
    device_mod = load_device_module(monkeypatch)

    assert device_mod._as_int("83") == 83
    assert device_mod._as_int(None) is None
    assert device_mod._as_int("junk") is None


@pytest.mark.parametrize(
    "model",
    [
        "roidmi.vacuum.r1b",
        "roborock.vacuum.a01",
        "dreame.vacuum.r2235a",
    ],
)
def test_device_refuses_non_onboardable_models(monkeypatch, model: str):
    device_mod = load_device_module(monkeypatch)

    with pytest.raises(ValueError):
        device_mod.IjaiVacuumDevice("host", "token", model)
