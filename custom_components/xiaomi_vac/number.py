"""Number: voice volume."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import XiaomiConfigEntry
from .const import DOMAIN, MAP_IDLE_INTERVAL
from .coordinator import XiaomiVacuumCoordinator
from .map_coordinator import XiaomiMapCoordinator

# Serialise commands to the device (one MIoT write at a time).
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: XiaomiConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.control
    entities = []
    if coordinator.device.core.volume is not None:
        entities.append(VolumeNumber(coordinator, entry))
    map_coordinator = entry.runtime_data.map
    if map_coordinator is not None:
        entities.append(MapIdleSafetyNetNumber(map_coordinator, entry))
    async_add_entities(entities)


# Spec-verified volume value-ranges (Phase 0 of the parity plan, verified
# 03-07-2026 via miot-spec.org). ijai/xiaomi/viomi cores use 0-10
# (ijai.v17 hardware-verified); dreame's audio service volume is 0-100
# (all 6 distinct dreame cores checked: p2008/p2009/p2114a/p2149o/p2150a/
# r2215). xiaomi.ov21gl/ov31gl/ov71gl are the JSON-map xiaomi profiles
# (E1/E3/E8) and diverge from the xiaomi brand default at 0-100 — checked
# first. xiaomi.c107 is hardware-confirmed at 0-100 (issue #18).
_VOLUME_MAX_BY_PROFILE_ID = {
    "xiaomi.c107": 100,
    "xiaomi.ov21gl": 100,
    "xiaomi.ov31gl": 100,
    "xiaomi.ov71gl": 100,
}
_VOLUME_MAX_BY_BRAND = {"dreame": 100}
_VOLUME_MAX_DEFAULT = 10


class VolumeNumber(CoordinatorEntity[XiaomiVacuumCoordinator], NumberEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_volume"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})
        profile = coordinator.device.profile
        self._attr_native_max_value = (
            _VOLUME_MAX_BY_PROFILE_ID.get(profile.profile_id)
            or _VOLUME_MAX_BY_BRAND.get(profile.brand, _VOLUME_MAX_DEFAULT)
        )

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.volume_raw

    async def async_set_native_value(self, value: float) -> None:
        await self.hass.async_add_executor_job(
            self.coordinator.device.set_volume, int(value)
        )
        await self.coordinator.async_request_refresh()


class MapIdleSafetyNetNumber(
    RestoreEntity, CoordinatorEntity[XiaomiMapCoordinator], NumberEntity
):
    """How often the map refreshes from the cloud while docked/idle, as a
    safety net independent of whether anyone's viewing it (issue #76).

    Covers consumers that read coordinator.data without ever triggering an
    on-demand refresh themselves (diagnostics, the room list), and — unless
    the idle on-demand-refresh switch is on — viewers too. Purely a local
    preference, not a device property, so its state is restored across
    restarts rather than read from the vacuum.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "map_idle_safety_net"
    _attr_icon = "mdi:timer-sand"
    _attr_native_min_value = 1
    _attr_native_max_value = 1440  # 24h ceiling
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: XiaomiMapCoordinator, entry) -> None:
        super().__init__(coordinator)
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_map_idle_safety_net"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, base)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        try:
            minutes = float(last_state.state) if last_state else None
        except (TypeError, ValueError):
            minutes = None
        if minutes is None:
            minutes = MAP_IDLE_INTERVAL / 60
        self.coordinator.idle_safety_net_seconds = minutes * 60
        self.coordinator._tune_interval()

    @property
    def native_value(self) -> float:
        return self.coordinator.idle_safety_net_seconds / 60

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.idle_safety_net_seconds = value * 60
        self.coordinator._tune_interval()
        self.async_write_ha_state()
