"""Harness tests for async_setup_entry: success, comm failure, map failure."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryNotReady
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_vac import PLATFORMS, async_setup_entry
from custom_components.xiaomi_vac.const import (
    CONF_HOST,
    CONF_MODEL,
    CONF_SERVICE_TOKEN,
    CONF_TOKEN,
    DOMAIN,
)
from custom_components.xiaomi_vac.device import DeviceCommunicationError, VacuumStatus

TOKEN = "0" * 32

_BASE_DATA = {
    CONF_HOST: "1.2.3.4",
    CONF_TOKEN: TOKEN,
    CONF_MODEL: "dreame.vacuum.p2008",
}

_STATUS = VacuumStatus(
    activity="docked",
    raw_status=0,
    battery=100,
    fault=0,
    fan_speed_raw=1,
    water_level_raw=None,
    mode_raw=None,
    sweep_type_raw=None,
    repeat_raw=None,
    alarm_raw=None,
    volume_raw=None,
    main_brush_life=100,
    side_brush_life=100,
    filter_life=100,
    mop_life=None,
    clean_area=0,
    clean_time=0,
)


def _fake_device(model: str = "dreame.vacuum.p2008"):
    """Minimal mock IjaiVacuumDevice with a dreame-like core."""
    device = MagicMock()
    device.model = model
    device.status.return_value = _STATUS
    core = MagicMock()
    core.charge = MagicMock()
    core.locate = None
    core.alarm = None
    core.repeat = None
    core.volume = None
    core.fan_speeds = {"quiet": 1, "normal": 2}
    core.water_levels = None
    core.modes = None
    core.sweep_types = None
    device.core = core
    return device


def _entry(uid: str, data: dict) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, version=2, unique_id=uid, data=data)
    return e


async def test_setup_entry_success_forwards_all_platforms(hass: HomeAssistant) -> None:
    """First-refresh succeeds → async_forward_entry_setups called with all platforms."""
    entry = _entry("AA:BB:CC:DD:EE:01", _BASE_DATA)
    entry.add_to_hass(hass)

    fake_device = _fake_device()
    mock_forward = AsyncMock(return_value=True)

    with (
        patch("custom_components.xiaomi_vac.IjaiVacuumDevice", return_value=fake_device),
        patch(
            "custom_components.xiaomi_vac.coordinator.XiaomiVacuumCoordinator"
            ".async_config_entry_first_refresh",
            new=AsyncMock(),
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=mock_forward),
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    mock_forward.assert_awaited_once_with(entry, PLATFORMS)


async def test_setup_entry_comm_failure_raises_not_ready(hass: HomeAssistant) -> None:
    """DeviceCommunicationError during first-refresh → ConfigEntryNotReady raised."""
    entry = _entry("AA:BB:CC:DD:EE:02", _BASE_DATA)
    entry.add_to_hass(hass)

    fake_device = _fake_device()
    fake_device.status.side_effect = DeviceCommunicationError("network timeout")

    # Let the real coordinator run its first refresh (which wraps status() →
    # UpdateFailed → ConfigEntryNotReady). We only need to bypass the HA guard
    # that refuses async_config_entry_first_refresh outside an entry context.
    from homeassistant.helpers.update_coordinator import UpdateFailed as _UF

    async def _first_refresh(self_coord):
        # Replicate the ConfigEntryNotReady escalation without needing HA wiring.
        try:
            await self_coord._async_update_data()
        except _UF as err:
            raise ConfigEntryNotReady from err

    with (
        patch("custom_components.xiaomi_vac.IjaiVacuumDevice", return_value=fake_device),
        patch(
            "custom_components.xiaomi_vac.coordinator.XiaomiVacuumCoordinator"
            ".async_config_entry_first_refresh",
            new=_first_refresh,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", new=AsyncMock()),
        pytest.raises(ConfigEntryNotReady),
    ):
        await async_setup_entry(hass, entry)


async def test_setup_entry_map_failure_does_not_block_control(hass: HomeAssistant) -> None:
    """Map coordinator first-refresh failure must not prevent the entry loading."""
    data = {**_BASE_DATA, CONF_SERVICE_TOKEN: "tok"}
    entry = _entry("AA:BB:CC:DD:EE:03", data)
    entry.add_to_hass(hass)

    fake_device = _fake_device()

    with (
        patch("custom_components.xiaomi_vac.IjaiVacuumDevice", return_value=fake_device),
        patch(
            "custom_components.xiaomi_vac.coordinator.XiaomiVacuumCoordinator"
            ".async_config_entry_first_refresh",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(return_value=True),
        ),
        # Patch the internal data-fetch method so the coordinator's own
        # async_refresh catches UpdateFailed and swallows it (HA behaviour).
        patch(
            "custom_components.xiaomi_vac.XiaomiMapCoordinator._async_update_data",
            new=AsyncMock(side_effect=UpdateFailed("map unavailable")),
        ),
    ):
        result = await async_setup_entry(hass, entry)

    # The map failure is swallowed by async_refresh; control setup must succeed.
    assert result is True


async def test_setup_entry_without_oauth_has_no_mqtt_client(hass: HomeAssistant) -> None:
    """Entry with no OAuth credentials must not start an MQTT client."""
    # _BASE_DATA has no CONF_OAUTH_* keys → _async_start_mqtt returns None.
    entry = _entry("AA:BB:CC:DD:EE:04", _BASE_DATA)
    entry.add_to_hass(hass)

    fake_device = _fake_device()

    with (
        patch("custom_components.xiaomi_vac.IjaiVacuumDevice", return_value=fake_device),
        patch(
            "custom_components.xiaomi_vac.coordinator.XiaomiVacuumCoordinator"
            ".async_config_entry_first_refresh",
            new=AsyncMock(),
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    assert entry.runtime_data.mqtt is None
