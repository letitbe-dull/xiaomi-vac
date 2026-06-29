"""Vacuum entity for Xiaomi (ijai-family) vacuums."""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN
from .coordinator import XiaomiVacuumCoordinator

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1

_ACTIVITY = {
    "cleaning": VacuumActivity.CLEANING,
    "paused": VacuumActivity.PAUSED,
    "idle": VacuumActivity.IDLE,
    "returning": VacuumActivity.RETURNING,
    "docked": VacuumActivity.DOCKED,
    "error": VacuumActivity.ERROR,
}

_BASE_SUPPORT = (
    VacuumEntityFeature.START
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.STATE
)


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaomiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    async_add_entities([XiaomiVacuum(coordinator, entry)])

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        "clean_segment",
        {vol.Required("segments"): vol.All(cv.ensure_list, [vol.Coerce(int)])},
        "async_clean_segment",
    )


class XiaomiVacuum(CoordinatorEntity[XiaomiVacuumCoordinator], StateVacuumEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: XiaomiVacuumCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._device = coordinator.device
        core = self._device.core
        support = _BASE_SUPPORT
        if core.charge is not None:
            support |= VacuumEntityFeature.RETURN_HOME
        if core.locate is not None or core.alarm is not None:
            support |= VacuumEntityFeature.LOCATE
        self._attr_supported_features = support
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_vacuum"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, base)},
            manufacturer="Xiaomi",
            model=self._device.model,
            name=entry.title,
        )

    @property
    def activity(self) -> VacuumActivity:
        return _ACTIVITY.get(self.coordinator.data.activity, VacuumActivity.IDLE)

    @property
    def extra_state_attributes(self) -> dict:
        return {"fault": self.coordinator.data.fault, "model": self._device.model}

    async def async_start(self) -> None:
        await self.hass.async_add_executor_job(self._device.start)
        await self.coordinator.async_request_refresh()

    async def async_stop(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self._device.stop)
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self._device.return_home)
        await self.coordinator.async_request_refresh()

    async def async_pause(self) -> None:
        await self.hass.async_add_executor_job(self._device.pause)
        await self.coordinator.async_request_refresh()

    async def async_locate(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self._device.locate)

    async def async_clean_segment(self, segments: list[int]) -> None:
        """Clean one or more rooms by their map room id (tap-to-clean)."""
        await self.hass.async_add_executor_job(self._device.clean_segments, segments)
        await self.coordinator.async_request_refresh()
