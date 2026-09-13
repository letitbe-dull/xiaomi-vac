"""Base-station action buttons."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN
from .coordinator import XiaomiVacuumCoordinator
from .spec.types import BaseStationCapability

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaomiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    entities = []
    if coordinator.device.core.locate is not None:
        entities.append(BaseActionButton(coordinator, entry, "find_vacuum", "mdi:bell-ring", "locate"))

    cap = coordinator.device.profile.base_station
    if not isinstance(cap, BaseStationCapability):
        async_add_entities(entities)
        return

    if cap.empty_dust_bin is not None:
        entities.append(BaseActionButton(coordinator, entry, "empty_dust_bin", "mdi:delete-sweep", "empty_dust_bin"))
    if cap.start_mop_wash is not None:
        entities.append(BaseActionButton(coordinator, entry, "wash_mops", "mdi:water-sync", "start_mop_wash"))
    async_add_entities(entities)


class BaseActionButton(CoordinatorEntity[XiaomiVacuumCoordinator], ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key: str, icon: str, method: str) -> None:
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_{key}"
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})
        self._method = method

    async def async_press(self) -> None:
        await self.hass.async_add_executor_job(getattr(self.coordinator.device, self._method))
        await self.coordinator.async_request_refresh()