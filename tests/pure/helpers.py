"""Helpers for pure tests that import integration modules without HA."""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


class FakeMiotDevice:
    """Small MiotDevice fake with scriptable reads/actions."""

    instances: list["FakeMiotDevice"] = []
    property_values: dict[tuple[int, int], object] = {}
    action_results: dict[tuple[int, int], object] = {}
    info_mac = "AA:BB:CC:DD:EE:FF"

    def __init__(self, host, token, timeout=5):  # noqa: ARG002
        self.calls = []
        self.instances.append(self)

    def get_property_by(self, siid: int, piid: int):
        self.calls.append(("get", siid, piid))
        value = self.property_values.get((siid, piid))
        if isinstance(value, Exception):
            raise value
        return [{"value": value}]

    def set_property_by(self, siid: int, piid: int, value):
        self.calls.append(("set", siid, piid, value))

    def call_action_by(self, siid: int, aiid: int, params):
        self.calls.append(("action", siid, aiid, params))
        result = self.action_results.get((siid, aiid), {"out": []})
        if isinstance(result, Exception):
            raise result
        return result

    def info(self):
        return SimpleNamespace(mac_address=self.info_mac)

    @classmethod
    def reset(cls) -> None:
        cls.instances.clear()
        cls.property_values = {}
        cls.action_results = {}
        cls.info_mac = "AA:BB:CC:DD:EE:FF"


def load_device_module(monkeypatch: pytest.MonkeyPatch):
    """Load xiaomi_vac.device with miio replaced by FakeMiotDevice."""
    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "xiaomi_vac"

    miio = ModuleType("miio")
    miio.MiotDevice = FakeMiotDevice
    monkeypatch.setitem(sys.modules, "miio", miio)

    pkg = ModuleType("xiaomi_vac")
    pkg.__path__ = [str(pkg_root)]
    monkeypatch.setitem(sys.modules, "xiaomi_vac", pkg)

    for name in list(sys.modules):
        if name == "xiaomi_vac.device" or name.startswith("xiaomi_vac.spec"):
            monkeypatch.delitem(sys.modules, name, raising=False)

    FakeMiotDevice.reset()
    return importlib.import_module("xiaomi_vac.device")


def load_sensor_module(monkeypatch: pytest.MonkeyPatch):
    """Load xiaomi_vac.sensor with HA replaced by thin stubs.

    Only the symbols actually consumed by sensor.py are stubbed — enough to
    import the module and exercise build_sensors() without a real HA install.
    """

    # --- minimal HA dataclass stubs ---

    @dataclass(frozen=True, kw_only=True)
    class _SensorEntityDescription:
        key: str = ""
        translation_key: str | None = None
        device_class: str | None = None
        options: list | None = None
        native_unit_of_measurement: str | None = None
        state_class: str | None = None
        entity_category: str | None = None

    class _SensorDeviceClass(SimpleNamespace):
        ENUM = "enum"
        BATTERY = "battery"

    class _SensorStateClass(SimpleNamespace):
        MEASUREMENT = "measurement"

    class _EntityCategory(SimpleNamespace):
        DIAGNOSTIC = "diagnostic"

    class _UnitOfArea(SimpleNamespace):
        SQUARE_METERS = "m²"

    class _UnitOfTime(SimpleNamespace):
        MINUTES = "min"

    # Stub modules sensor.py imports from HA
    def _make(name: str, **attrs) -> ModuleType:
        m = ModuleType(name)
        m.__dict__.update(attrs)
        return m

    sensor_mod = _make(
        "homeassistant.components.sensor",
        SensorDeviceClass=_SensorDeviceClass,
        SensorEntity=object,
        SensorEntityDescription=_SensorEntityDescription,
        SensorStateClass=_SensorStateClass,
    )
    const_mod = _make(
        "homeassistant.const",
        PERCENTAGE="%",
        EntityCategory=_EntityCategory,
        UnitOfArea=_UnitOfArea,
        UnitOfTime=_UnitOfTime,
    )
    for mod_name, mod in [
        ("homeassistant", ModuleType("homeassistant")),
        ("homeassistant.components", ModuleType("homeassistant.components")),
        ("homeassistant.components.sensor", sensor_mod),
        ("homeassistant.const", const_mod),
        ("homeassistant.core", _make("homeassistant.core", HomeAssistant=object)),
        ("homeassistant.helpers", ModuleType("homeassistant.helpers")),
        ("homeassistant.helpers.device_registry", _make("homeassistant.helpers.device_registry", DeviceInfo=object)),
        ("homeassistant.helpers.entity_platform", _make("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)),
        ("homeassistant.helpers.update_coordinator", _make(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=type("CoordinatorEntity", (), {"__class_getitem__": classmethod(lambda cls, _: cls)}),
            DataUpdateCoordinator=object,
            UpdateFailed=Exception,
        )),
    ]:
        monkeypatch.setitem(sys.modules, mod_name, mod)

    # Stub the integration's own modules that sensor.py depends on
    pkg_root = Path(__file__).resolve().parents[2] / "custom_components" / "xiaomi_vac"
    pkg = ModuleType("xiaomi_vac")
    pkg.__path__ = [str(pkg_root)]
    monkeypatch.setitem(sys.modules, "xiaomi_vac", pkg)

    # __init__ stub (just XiaomiConfigEntry)
    init_stub = ModuleType("xiaomi_vac.__init__")
    init_stub.XiaomiConfigEntry = object
    monkeypatch.setitem(sys.modules, "xiaomi_vac.__init__", init_stub)
    # The relative `. import XiaomiConfigEntry` resolves via the package __init__
    pkg.XiaomiConfigEntry = object  # type: ignore[attr-defined]

    const_stub = _make("xiaomi_vac.const", DOMAIN="xiaomi_vac")
    monkeypatch.setitem(sys.modules, "xiaomi_vac.const", const_stub)

    coordinator_stub = _make(
        "xiaomi_vac.coordinator",
        XiaomiVacuumCoordinator=object,
    )
    monkeypatch.setitem(sys.modules, "xiaomi_vac.coordinator", coordinator_stub)

    # device stub — only VacuumStatus is needed by sensor.py
    device_stub = _make("xiaomi_vac.device", VacuumStatus=object)
    monkeypatch.setitem(sys.modules, "xiaomi_vac.device", device_stub)

    # Ensure spec subpackage stubs are present so relative imports resolve
    spec_pkg = ModuleType("xiaomi_vac.spec")
    spec_pkg.__path__ = [str(pkg_root / "spec")]
    monkeypatch.setitem(sys.modules, "xiaomi_vac.spec", spec_pkg)

    # spec.types is imported directly by sensor.py; import the real thing
    for name in list(sys.modules):
        if name == "xiaomi_vac.spec.types" or name == "xiaomi_vac.sensor":
            monkeypatch.delitem(sys.modules, name, raising=False)

    # Import real spec.types so ModelProfile etc. are genuine objects
    spec_types = importlib.import_module("xiaomi_vac.spec.types")
    monkeypatch.setitem(sys.modules, "xiaomi_vac.spec.types", spec_types)

    return importlib.import_module("xiaomi_vac.sensor")
