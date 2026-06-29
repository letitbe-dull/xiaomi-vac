"""Selects: fan speed, water level, cleaning mode, sweep type."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN
from .coordinator import XiaomiVacuumCoordinator

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1

# (key, core attr holding {name: raw}, VacuumStatus attr with current raw, device setter)
SELECTS = (
    ("fan_speed", "fan_speeds", "fan_speed_raw", "set_fan_speed"),
    ("water_level", "water_levels", "water_level_raw", "set_water_level"),
    ("mode", "modes", "mode_raw", "set_mode"),
    ("sweep_type", "sweep_types", "sweep_type_raw", "set_sweep_type"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaomiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    core = coordinator.device.core
    # Build only the selects whose core value table exists for this model —
    # no empty "sweep type" dropdown on a model that lacks it (e.g. dreame).
    async_add_entities(
        XiaomiVacuumSelect(coordinator, entry, *cfg)
        for cfg in SELECTS
        if getattr(core, cfg[1])
    )


class XiaomiVacuumSelect(CoordinatorEntity[XiaomiVacuumCoordinator], SelectEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key, spec_attr, status_attr, setter):
        super().__init__(coordinator)
        self._key = key
        self._status_attr = status_attr
        self._setter = setter
        self._options_map: dict[str, int] = getattr(coordinator.device.core, spec_attr)
        self._reverse = {v: k for k, v in self._options_map.items()}
        self._attr_translation_key = key
        self._attr_options = list(self._options_map)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def current_option(self) -> str | None:
        return self._reverse.get(getattr(self.coordinator.data, self._status_attr))

    async def async_select_option(self, option: str) -> None:
        setter = getattr(self.coordinator.device, self._setter)
        await self.hass.async_add_executor_job(setter, option)
        await self.coordinator.async_request_refresh()
