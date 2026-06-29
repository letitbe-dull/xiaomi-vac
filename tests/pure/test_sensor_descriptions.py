"""Pure tests for build_sensors() — no HA install required."""
from __future__ import annotations

import pytest

from .helpers import load_sensor_module


_DEAD_KEYS = {
    "clean_area",
    "clean_time",
    "main_brush_life",
    "side_brush_life",
    "filter_life",
    "mop_life",
}


def _ijai_v17_profile(monkeypatch):
    """Return the real IJAI_V17 ModelProfile (no HA needed)."""
    import importlib
    import sys
    from pathlib import Path
    from types import ModuleType

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

    profiles_mod = importlib.import_module("xiaomi_vac.spec.profiles.ijai")
    return profiles_mod.IJAI_V17


def test_build_sensors_ijai_v17_has_status_and_battery(monkeypatch):
    sensor = load_sensor_module(monkeypatch)
    profile = _ijai_v17_profile(monkeypatch)

    sensors = sensor.build_sensors(profile)
    keys = {d.key for d in sensors}

    assert "status" in keys
    assert "battery" in keys


def test_build_sensors_ijai_v17_no_dead_sensors(monkeypatch):
    """ijai.v17 must not produce permanently-None sensor entities at launch."""
    sensor = load_sensor_module(monkeypatch)
    profile = _ijai_v17_profile(monkeypatch)

    sensors = sensor.build_sensors(profile)
    keys = {d.key for d in sensors}

    assert keys.isdisjoint(_DEAD_KEYS), (
        f"Dead sensor(s) found for ijai.v17: {keys & _DEAD_KEYS}"
    )


def test_build_sensors_ijai_v17_exact_set(monkeypatch):
    """Exactly {status, battery} for ijai.v17 at launch (no extras, no missing)."""
    sensor = load_sensor_module(monkeypatch)
    profile = _ijai_v17_profile(monkeypatch)

    sensors = sensor.build_sensors(profile)
    keys = {d.key for d in sensors}

    assert keys == {"status", "battery"}


def test_build_sensors_profile_without_battery_omits_battery(monkeypatch):
    """A profile with core.battery=None must not get a battery sensor."""
    from dataclasses import replace

    sensor = load_sensor_module(monkeypatch)
    profile = _ijai_v17_profile(monkeypatch)
    # Strip the battery prop from the core
    coreless_battery = replace(profile.core, battery=None)
    no_battery_profile = replace(profile, core=coreless_battery)

    sensors = sensor.build_sensors(no_battery_profile)
    keys = {d.key for d in sensors}

    assert "battery" not in keys
    assert "status" in keys
