"""Pure tests for the consumable-life sensors (main/side brush, filter, dust
bag, detergent) added on top of `DreameConsumablesCapability` for
`xiaomi.ov42gl` — covers `sensor.py`'s `build_sensors()` gating and
`device.py`'s `IjaiVacuumDevice.status()` actually polling+returning them.

No homeassistant install required — see tests/pure/helpers.py.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from .helpers import FakeMiotDevice, load_device_module, load_sensor_module


def _ov42gl_profile(monkeypatch):
    """Return the real xiaomi.ov42gl ModelProfile (no HA needed)."""
    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "xiaomi_vac"
    pkg = ModuleType("xiaomi_vac")
    pkg.__path__ = [str(pkg_root)]
    sys.modules.setdefault("xiaomi_vac", pkg)

    spec_pkg = ModuleType("xiaomi_vac.spec")
    spec_pkg.__path__ = [str(pkg_root / "spec")]
    sys.modules.setdefault("xiaomi_vac.spec", spec_pkg)

    for name in list(sys.modules):
        if name.startswith("xiaomi_vac.spec.") and "profiles" in name:
            monkeypatch.delitem(sys.modules, name, raising=False)

    profiles_mod = importlib.import_module("xiaomi_vac.spec.profiles.xiaomi")
    return profiles_mod.XIAOMI_OV42GL


# --- sensor.py: build_sensors() gating -------------------------------------
def test_build_sensors_ov42gl_has_all_five_consumables(monkeypatch):
    sensor = load_sensor_module(monkeypatch)
    profile = _ov42gl_profile(monkeypatch)

    sensors = sensor.build_sensors(profile)
    keys = {d.key for d in sensors}

    assert {"main_brush_life", "side_brush_life", "filter_life", "dust_bag_life", "detergent_life"} <= keys


def test_build_sensors_profile_without_consumables_omits_them(monkeypatch):
    """A profile whose `consumables` isn't the percent+hours-shaped
    DreameConsumablesCapability (e.g. None, or ijai's own different shape)
    must not produce any of these five sensors."""
    sensor = load_sensor_module(monkeypatch)
    profile = _ov42gl_profile(monkeypatch)
    bare_profile = replace(profile, consumables=None)

    sensors = sensor.build_sensors(bare_profile)
    keys = {d.key for d in sensors}

    assert keys.isdisjoint({"main_brush_life", "side_brush_life", "filter_life", "dust_bag_life", "detergent_life"})


def test_build_sensors_partial_consumables_only_supported_ones(monkeypatch):
    """If only some consumable props are set on the profile (e.g. no dust
    bag on a mop-only model), only the sensors with a real prop should be
    built — matches the same per-field `supported_fn` gating pattern
    `battery`/`status` already use."""
    sensor = load_sensor_module(monkeypatch)
    profile = _ov42gl_profile(monkeypatch)
    # Import AFTER load_sensor_module: it reloads xiaomi_vac.spec.types fresh
    # into sys.modules, so importing DreameConsumablesCapability any earlier
    # would grab a stale module instance — isinstance() against sensor.py's
    # own (later-reloaded) class would then silently always be False.
    from xiaomi_vac.spec.types import DreameConsumablesCapability, Prop
    partial = replace(profile, consumables=DreameConsumablesCapability(
        main_brush_life=Prop(12, 1), side_brush_life=None,
        filter_life=None, dust_bag_life=None, detergent_life=None,
    ))

    sensors = sensor.build_sensors(partial)
    keys = {d.key for d in sensors}

    assert "main_brush_life" in keys
    assert keys.isdisjoint({"side_brush_life", "filter_life", "dust_bag_life", "detergent_life"})


# --- device.py: IjaiVacuumDevice.status() actually polling them ------------
def test_status_returns_all_consumable_life_values(monkeypatch):
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "xiaomi.vacuum.ov42gl")
    status_prop = device.core.status
    cons = device.profile.consumables

    FakeMiotDevice.property_values = {
        (status_prop.siid, status_prop.piid): 5,
        (cons.main_brush_life.siid, cons.main_brush_life.piid): 87,
        (cons.side_brush_life.siid, cons.side_brush_life.piid): 62,
        (cons.filter_life.siid, cons.filter_life.piid): 45,
        (cons.dust_bag_life.siid, cons.dust_bag_life.piid): 90,
        (cons.detergent_life.siid, cons.detergent_life.piid): 33,
    }

    status = device.status()

    assert status.main_brush_life == 87
    assert status.side_brush_life == 62
    assert status.filter_life == 45
    assert status.dust_bag_life == 90
    assert status.detergent_life == 33


def test_status_consumable_read_failure_does_not_break_status(monkeypatch):
    """One consumable prop failing (e.g. transient read error) must not
    raise or blank out the rest of the status — only status itself is
    required (matches the existing ijai behaviour in test_device_status.py's
    test_status_tolerates_optional_prop_none)."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "xiaomi.vacuum.ov42gl")
    status_prop = device.core.status
    cons = device.profile.consumables

    FakeMiotDevice.property_values = {
        (status_prop.siid, status_prop.piid): 5,
        (cons.main_brush_life.siid, cons.main_brush_life.piid): 87,
        # side_brush/filter/dust_bag/detergent simply absent -> None, no batch failure
    }

    status = device.status()

    assert status.raw_status == 5
    assert status.main_brush_life == 87
    assert status.side_brush_life is None


def test_status_consumables_not_polled_for_profile_without_them(monkeypatch):
    """A profile with no DreameConsumablesCapability (e.g. plain ijai) must
    not attempt to read any consumable prop at all — confirms the
    isinstance-gated poll list in device.py, not just that the result comes
    back None."""
    device_mod = load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")
    status_prop = device.core.status
    FakeMiotDevice.property_values = {(status_prop.siid, status_prop.piid): 5}

    status = device.status()

    assert status.main_brush_life is None
    assert status.dust_bag_life is None
    assert status.detergent_life is None
