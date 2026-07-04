"""Buttons: user-triggered map refresh (Phase 4, map-reliability plan)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN
from .coordinator import XiaomiVacuumCoordinator
from .map_coordinator import XiaomiMapCoordinator

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1

# Only expose the button as available while the vacuum is here — anywhere else
# and pressing it either aborts a real clean or moves the vacuum unexpectedly.
_ELIGIBLE_ACTIVITIES = {"docked", "idle"}


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaomiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    map_coordinator = entry.runtime_data.map
    control = entry.runtime_data.control
    if map_coordinator is None:
        # No cloud session was captured -> no map subsystem -> refreshing it
        # makes no sense on a local-only entry.
        return
    async_add_entities([RefreshMapButton(map_coordinator, control, entry)])


class RefreshMapButton(CoordinatorEntity[XiaomiMapCoordinator], ButtonEntity):
    """Briefly undock the vacuum to force a fresh readable map upload.

    The vacuum will leave the dock and return. Never runs without an explicit
    user press; guards inside the coordinator prevent it from running while the
    vacuum is cleaning or already refreshing.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "refresh_map"
    _attr_icon = "mdi:map-search"

    def __init__(
        self,
        coordinator: XiaomiMapCoordinator,
        control: XiaomiVacuumCoordinator,
        entry: XiaomiConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._control = control
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_refresh_map"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    async def async_added_to_hass(self) -> None:
        # Availability tracks the vacuum's activity, which lives on the control
        # coordinator; subscribe so the button greys out the instant a clean
        # starts elsewhere (voice command, Mi Home, schedule).
        await super().async_added_to_hass()
        self.async_on_remove(
            self._control.async_add_listener(self._handle_control_update)
        )

    @callback
    def _handle_control_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if self.coordinator.refreshing:
            return False
        ctrl_data = self._control.data
        activity = ctrl_data.activity if ctrl_data is not None else None
        return activity in _ELIGIBLE_ACTIVITIES

    async def async_press(self) -> None:
        await self.coordinator.async_refresh_map_undock()
