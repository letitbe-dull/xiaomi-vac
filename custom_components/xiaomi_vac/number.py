"""Number: voice volume (0-10)."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN
from .coordinator import XiaomiVacuumCoordinator

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaomiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    if coordinator.device.core.volume is not None:
        async_add_entities([VolumeNumber(coordinator, entry)])


class VolumeNumber(CoordinatorEntity[XiaomiVacuumCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 10
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_volume"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.volume_raw

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.device.set_volume, int(value)
        )
        await self.coordinator.async_request_refresh()
