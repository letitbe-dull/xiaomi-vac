"""Sensors: battery + consumables + clean stats (control coordinator)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfArea, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN
from .coordinator import XiaomiVacuumCoordinator
from .device import VacuumStatus
from .spec.types import ModelProfile

# Read-only platform fed by the coordinator; no device writes to serialise.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class XiaomiSensorDescription(SensorEntityDescription):
    value_fn: Callable[[VacuumStatus], int | str | None]
    # Returns True when the profile actually exposes this sensor's data source.
    # None means always include (no capability gate).
    supported_fn: Callable[[ModelProfile], bool] | None = None


# Canonical sensor catalogue.  Sensors whose data the coordinator cannot yet
# populate (consumable-life, clean area/time — parked pending a dedicated
# coordinator) are omitted here; they will be re-added with a supported_fn
# predicate once the implementation is in place.
_ALL_SENSORS: tuple[XiaomiSensorDescription, ...] = (
    XiaomiSensorDescription(
        key="status", translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=["cleaning", "paused", "idle", "returning", "docked", "error"],
        value_fn=lambda s: s.activity,
        # status is always populated (required prop, raises on failure)
    ),
    XiaomiSensorDescription(
        key="battery", translation_key="battery",
        device_class=SensorDeviceClass.BATTERY, native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT, value_fn=lambda s: s.battery,
        supported_fn=lambda p: p.core is not None and p.core.battery is not None,
    ),
)


def build_sensors(profile: ModelProfile) -> tuple[XiaomiSensorDescription, ...]:
    """Return only the sensor descriptions supported by *profile*.

    Each descriptor with a ``supported_fn`` is tested against the profile;
    descriptors without one are always included.
    """
    return tuple(
        d for d in _ALL_SENSORS
        if d.supported_fn is None or d.supported_fn(profile)
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaomiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    sensors = build_sensors(coordinator.device.profile)
    async_add_entities(XiaomiVacuumSensor(coordinator, entry, d) for d in sensors)


class XiaomiVacuumSensor(CoordinatorEntity[XiaomiVacuumCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: XiaomiSensorDescription

    def __init__(self, coordinator, entry, description: XiaomiSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def native_value(self) -> int | str | None:
        return self.entity_description.value_fn(self.coordinator.data)
