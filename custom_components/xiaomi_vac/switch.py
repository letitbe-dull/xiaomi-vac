"""Switch: repeat (clean each area twice)."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN
from .coordinator import XiaomiVacuumCoordinator
from .spec.types import BaseStationCapability

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaomiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    core = coordinator.device.core
    entities = []
    if core.repeat is not None:
        entities.append(RepeatSwitch(coordinator, entry))
    if core.alarm is not None:
        entities.append(AlarmSwitch(coordinator, entry))
    base = coordinator.device.profile.base_station
    if isinstance(base, BaseStationCapability) and base.auto_mop_dry is not None:
        entities.append(AutoMopDrySwitch(coordinator, entry))
    if (
        isinstance(base, BaseStationCapability)
        and base.start_drying is not None
        and base.stop_drying is not None
        and base.working_status is not None
    ):
        entities.append(MopDryingSwitch(coordinator, entry))
    async_add_entities(entities)


class RepeatSwitch(CoordinatorEntity[XiaomiVacuumCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "repeat"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_repeat"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool | None:
        raw = self.coordinator.data.repeat_raw
        return None if raw is None else bool(raw)

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.set_repeat, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.set_repeat, False)
        await self.coordinator.async_request_refresh()


class AlarmSwitch(CoordinatorEntity[XiaomiVacuumCoordinator], SwitchEntity):
    """Beep the vacuum to find it (Alarm property)."""

    _attr_has_entity_name = True
    _attr_translation_key = "alarm"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_alarm"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool | None:
        raw = self.coordinator.data.alarm_raw
        return None if raw is None else bool(raw)

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.set_alarm, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.set_alarm, False)
        await self.coordinator.async_request_refresh()


class AutoMopDrySwitch(CoordinatorEntity[XiaomiVacuumCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "auto_mop_dry"
    _attr_icon = "mdi:heat-wave"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_auto_mop_dry"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.auto_mop_dry

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.device.set_auto_mop_dry, True
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.device.set_auto_mop_dry, False
        )
        await self.coordinator.async_request_refresh()


class MopDryingSwitch(CoordinatorEntity[XiaomiVacuumCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "mop_drying"
    _attr_icon = "mdi:heat-wave"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_mop_drying"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.base_station_mode == 1

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.start_drying)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.async_add_executor_job(self.coordinator.device.stop_drying)
        await self.coordinator.async_request_refresh()
