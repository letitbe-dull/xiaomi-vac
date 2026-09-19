"""Sensors: battery + consumables + clean stats (control coordinator)."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN
from .coordinator import XiaomiVacuumCoordinator
from .device import VacuumStatus
from .spec.types import ModelProfile, consumable_life_props

# Read-only platform fed by the coordinator; no device writes to serialise.
PARALLEL_UPDATES = 0

DOOR_STATES = {0: "none", 1: "dust_box", 2: "water_box", 3: "two_in_one"}


@dataclass(frozen=True, kw_only=True)
class XiaomiSensorDescription(SensorEntityDescription):
    value_fn: Callable[[VacuumStatus], int | str | None]
    # Returns True when the profile actually exposes this sensor's data source.
    # None means always include (no capability gate).
    supported_fn: Callable[[ModelProfile], bool] | None = None


def _has_consumable(attr: str) -> Callable[[ModelProfile], bool]:
    """Return a Model Profile consumable predicate."""
    return lambda p: consumable_life_props(p.consumables).get(attr) is not None


def _has_door_state(profile: ModelProfile) -> bool:
    """Return whether the Model Profile exposes verified door state."""
    return (
        profile.profile_id == "ijai.v17"
        and getattr(profile.consumables, "door_state", None) is not None
    )


# Canonical sensor catalogue.  Clean area/time still have no populating
# coordinator support and stay omitted; consumable-life (2026-08-01) is now
# wired end to end (device.py polls it, gated the same way below).
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
    XiaomiSensorDescription(
        key="door_state", translation_key="door_state", icon="mdi:cube-outline",
        device_class=SensorDeviceClass.ENUM,
        options=["none", "dust_box", "water_box", "two_in_one"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: DOOR_STATES.get(s.door_state_raw),
        supported_fn=_has_door_state,
    ),
    XiaomiSensorDescription(
        key="main_brush_life", translation_key="main_brush_life", icon="mdi:broom",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.main_brush_life,
        supported_fn=_has_consumable("main_brush_life"),
    ),
    XiaomiSensorDescription(
        key="side_brush_life", translation_key="side_brush_life", icon="mdi:broom",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.side_brush_life,
        supported_fn=_has_consumable("side_brush_life"),
    ),
    XiaomiSensorDescription(
        key="filter_life", translation_key="filter_life", icon="mdi:air-filter",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.filter_life,
        supported_fn=_has_consumable("filter_life"),
    ),
    XiaomiSensorDescription(
        key="mop_life", translation_key="mop_life", icon="mdi:layers-triple-outline",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.mop_life,
        supported_fn=_has_consumable("mop_life"),
    ),
    XiaomiSensorDescription(
        key="dust_bag_life", translation_key="dust_bag_life", icon="mdi:trash-can-outline",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.dust_bag_life,
        supported_fn=_has_consumable("dust_bag_life"),
    ),
    XiaomiSensorDescription(
        key="detergent_life", translation_key="detergent_life", icon="mdi:cup-water",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC, value_fn=lambda s: s.detergent_life,
        supported_fn=_has_consumable("detergent_life"),
    ),
    XiaomiSensorDescription(
        key="drying_progress", translation_key="drying_progress", icon="mdi:progress-clock",
        native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.drying_progress,
        supported_fn=lambda p: p.base_station is not None and p.base_station.drying_progress is not None,
    ),
    XiaomiSensorDescription(
        key="dry_left_time", translation_key="dry_left_time", icon="mdi:timer-sand",
        native_unit_of_measurement=UnitOfTime.MINUTES, state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.dry_left_time,
        supported_fn=lambda p: p.base_station is not None and p.base_station.dry_left_time is not None,
    ),
    XiaomiSensorDescription(
        key="base_station_status", translation_key="base_station_status",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "drying", "washing_mops", "dust_collection", "unknown"],
        value_fn=lambda s: s.base_station_status,
        supported_fn=lambda p: p.base_station is not None and p.base_station.working_status is not None,
    ),
    XiaomiSensorDescription(
        key="sewage_tank_status", translation_key="sewage_tank_status",
        device_class=SensorDeviceClass.ENUM,
        options=["not_full", "full", "unknown"],
        value_fn=lambda s: {
            0: "not_full",
            1: "full",
        }.get(s.sewage_tank_status, "unknown"),
        supported_fn=lambda p: p.base_station is not None and p.base_station.sewage_tank_status is not None,
    ),
    XiaomiSensorDescription(
        key="water_tank_status", translation_key="water_tank_status",
        device_class=SensorDeviceClass.ENUM,
        options=["not_empty", "empty", "unknown"],
        value_fn=lambda s: {
            0: "not_empty",
            1: "empty",
        }.get(s.water_tank_status, "unknown"),
        supported_fn=lambda p: p.base_station is not None and p.base_station.water_tank_status is not None,
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
