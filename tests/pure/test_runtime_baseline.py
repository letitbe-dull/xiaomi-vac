"""Pure-tier tests for the Phase 10 card baseline."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

from spec.registry import MODEL_PROFILES, card_baseline_gaps, get_profile, is_supported
from spec.types import Action, Prop


def test_is_supported_matches_card_baseline_for_registry():
    mismatches = {
        model: card_baseline_gaps(profile)
        for model, profile in MODEL_PROFILES.items()
        if is_supported(model) != (not card_baseline_gaps(profile))
    }

    assert mismatches == {}


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("ijai.vacuum.v17", True),
        ("dreame.vacuum.p2008", True),
        ("viomi.vacuum.v12", True),
        ("viomi.vacuum.v45", True),
        ("dreame.vacuum.r2235a", False),
        ("roidmi.vacuum.r1b", False),
        ("roborock.vacuum.a01", False),
    ],
)
def test_support_gate_known_models(model, expected):
    assert is_supported(model) is expected


def test_dreame_core_lifts_card_controls():
    profile = get_profile("dreame.vacuum.p2008")
    core = profile.core

    assert core.fan_speed == Prop(4, 4)
    assert core.water_level == Prop(4, 5)
    assert core.locate == Action(7, 1)
    assert core.fan_speeds == {
        "Quiet": 0,
        "Standard": 1,
        "Medium Gear": 2,
        "Strong": 3,
    }
    assert core.water_levels == {
        "Low Water Level": 1,
        "Medium Water Level": 2,
        "High Water Level": 3,
    }


def test_viomi_v45_uses_ijai_shaped_core_for_card_controls():
    profile = get_profile("viomi.vacuum.v45")
    core = profile.core

    assert core.charge == Action(3, 1)
    assert core.fan_speed == Prop(7, 5)
    assert core.water_level == Prop(7, 6)
    assert core.alarm == Prop(4, 1)
    assert card_baseline_gaps(profile) == ()


def test_trimmed_dreame_layout_is_not_onboardable():
    profile = get_profile("dreame.vacuum.r2235a")

    assert set(card_baseline_gaps(profile)) >= {"fan_speed", "water_level", "locate"}


class _FakeMiotDevice:
    instances: list["_FakeMiotDevice"] = []

    def __init__(self, host, token, timeout=5):
        self.calls = []
        self.instances.append(self)

    def set_property_by(self, siid, piid, value):
        self.calls.append(("set", siid, piid, value))

    def call_action_by(self, siid, aiid, params):
        self.calls.append(("action", siid, aiid, params))
        return {"out": []}


def _load_device_module(monkeypatch: pytest.MonkeyPatch):
    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "xiaomi_vac"

    miio = ModuleType("miio")
    miio.MiotDevice = _FakeMiotDevice
    monkeypatch.setitem(sys.modules, "miio", miio)

    pkg = ModuleType("xiaomi_vac")
    pkg.__path__ = [str(pkg_root)]
    monkeypatch.setitem(sys.modules, "xiaomi_vac", pkg)

    for name in list(sys.modules):
        if name == "xiaomi_vac.device" or name.startswith("xiaomi_vac.spec"):
            monkeypatch.delitem(sys.modules, name, raising=False)

    _FakeMiotDevice.instances.clear()
    return importlib.import_module("xiaomi_vac.device")


def test_device_locate_uses_core_action(monkeypatch):
    device_mod = _load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "dreame.vacuum.p2008")

    device.locate()

    assert _FakeMiotDevice.instances[-1].calls == [("action", 7, 1, [])]


def test_device_locate_falls_back_to_alarm_property(monkeypatch):
    device_mod = _load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "ijai.vacuum.v17")

    device.locate()

    assert _FakeMiotDevice.instances[-1].calls == [("set", 4, 1, True)]


def test_device_clean_segments_uses_viomi_set_room_clean(monkeypatch):
    device_mod = _load_device_module(monkeypatch)
    device = device_mod.IjaiVacuumDevice("host", "token", "viomi.vacuum.v12")

    device.clean_segments([101, 102])

    assert _FakeMiotDevice.instances[-1].calls == [
        ("action", 4, 13, [0, 1, "101,102"])
    ]


def test_device_rejects_profiles_below_card_baseline(monkeypatch):
    device_mod = _load_device_module(monkeypatch)

    with pytest.raises(ValueError, match="card baseline"):
        device_mod.IjaiVacuumDevice("host", "token", "dreame.vacuum.r2235a")
