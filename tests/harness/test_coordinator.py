"""Harness tests for XiaomiVacuumCoordinator and XiaomiMapCoordinator."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.xiaomi_vac.const import (
    CONF_DEVICE_ID,
    CONF_MAC,
    CONF_MODEL,
    CONF_PASS_TOKEN,
    CONF_PASSWORD,
    CONF_SERVER,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
    CONF_WIFI_SN,
)
from custom_components.xiaomi_vac.coordinator import XiaomiVacuumCoordinator
from custom_components.xiaomi_vac.device import DeviceCommunicationError
from custom_components.xiaomi_vac.map import SessionExpired
from custom_components.xiaomi_vac.map_coordinator import XiaomiMapCoordinator


async def test_coordinator_raises_update_failed_on_communication_error(
    hass: HomeAssistant,
) -> None:
    """DeviceCommunicationError from device.status() must become UpdateFailed."""
    entry = MagicMock()
    device = MagicMock()
    device.model = "ijai.vacuum.v17"
    device.status.side_effect = DeviceCommunicationError("network timeout")

    coordinator = XiaomiVacuumCoordinator(hass, entry, device)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_coordinator_returns_status_on_success(
    hass: HomeAssistant,
) -> None:
    """Successful device.status() result passes through the coordinator."""
    entry = MagicMock()
    device = MagicMock()
    device.model = "ijai.vacuum.v17"
    fake_status = MagicMock()
    device.status.return_value = fake_status

    coordinator = XiaomiVacuumCoordinator(hass, entry, device)

    result = await coordinator._async_update_data()

    assert result is fake_status


def _map_entry(model: str) -> MagicMock:
    """Minimal config entry for map coordinator tests."""
    entry = MagicMock()
    entry.data = {
        CONF_MODEL: model,
        CONF_USERNAME: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_USER_ID: "12345",
        CONF_SSECURITY: "ssec",
        CONF_SERVICE_TOKEN: "svctoken",
        CONF_DEVICE_ID: "did123",
        CONF_SERVER: "cn",
        CONF_WIFI_SN: "",  # deliberately absent
        CONF_MAC: "",      # deliberately absent
    }
    return entry


@pytest.mark.parametrize("model", ["xiaomi.vacuum.c101", "dreame.vacuum.p2008", "viomi.vacuum.v18"])
async def test_map_coordinator_build_non_ijai_does_not_require_wifi_sn(
    hass: HomeAssistant,
    model: str,
) -> None:
    """Non-ijai brands must not raise UpdateFailed just because wifi_sn is absent."""
    entry = _map_entry(model)
    device = MagicMock()
    control = MagicMock()
    control.data = None

    coord = XiaomiMapCoordinator(hass, entry, device, control)

    fake_fetcher = MagicMock()
    fake_fetcher.fetch_all.return_value = MagicMock()

    with (
        patch("custom_components.xiaomi_vac.map_coordinator.XiaomiCloud"),
        patch.object(coord, "_build", return_value=fake_fetcher),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=lambda fn, *a: fn(*a))),
    ):
        # _build is patched so _async_update_data must not raise UpdateFailed
        # due to missing wifi_sn/mac; any failure here is a regression.
        result = await coord._async_update_data()

    assert result is not None


async def test_map_coordinator_build_ijai_raises_without_wifi_sn(
    hass: HomeAssistant,
) -> None:
    """ijai _build must raise UpdateFailed when wifi_sn is unavailable."""
    entry = _map_entry("ijai.vacuum.v17")
    device = MagicMock()
    device.get_wifi_sn.return_value = ""   # live read fails
    device.get_mac.return_value = "aa:bb:cc:dd:ee:ff"
    control = MagicMock()
    control.data = None

    coord = XiaomiMapCoordinator(hass, entry, device, control)

    with (
        patch("custom_components.xiaomi_vac.map_coordinator.XiaomiCloud"),
        pytest.raises(UpdateFailed, match="wifi_sn"),
    ):
        coord._build()


# ---------------------------------------------------------------------------
# Map coordinator: _async_update_data advanced paths
# ---------------------------------------------------------------------------


def _map_coord(hass: HomeAssistant, model: str = "dreame.vacuum.p2008") -> XiaomiMapCoordinator:
    """Build a coordinator with a pre-set _fetcher mock."""
    entry = _map_entry(model)
    device = MagicMock()
    device.map_list.return_value = []
    control = MagicMock()
    control.data = None
    coord = XiaomiMapCoordinator(hass, entry, device, control)
    return coord


async def test_map_coordinator_session_expiry_triggers_pass_token_refresh(
    hass: HomeAssistant,
) -> None:
    """SessionExpired from fetch_all → _refresh_and_persist called, retry succeeds."""
    coord = _map_coord(hass)

    fake_result = MagicMock()
    fetcher = MagicMock()
    # First call raises SessionExpired; second (after refresh) returns a result.
    fetcher.fetch_all.side_effect = [SessionExpired("token dead"), fake_result]
    coord._fetcher = fetcher

    async def _exec(fn, *a):
        return fn(*a)

    mock_refresh = AsyncMock(return_value=True)
    with (
        patch.object(coord, "_refresh_and_persist", new=mock_refresh),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        result = await coord._async_update_data()

    assert result is fake_result
    mock_refresh.assert_awaited_once()


async def test_map_coordinator_session_expiry_refresh_fails_raises_auth_failed(
    hass: HomeAssistant,
) -> None:
    """If passToken refresh itself fails, ConfigEntryAuthFailed must be raised."""
    coord = _map_coord(hass)

    fetcher = MagicMock()
    fetcher.fetch_all.side_effect = SessionExpired("dead")
    coord._fetcher = fetcher

    async def _exec(fn, *a):
        return fn(*a)

    with (
        patch.object(coord, "_refresh_and_persist", new=AsyncMock(return_value=False)),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
        pytest.raises(ConfigEntryAuthFailed),
    ):
        await coord._async_update_data()


async def test_map_coordinator_none_result_resets_fetcher_and_raises(
    hass: HomeAssistant,
) -> None:
    """fetch_all returning None (bad AES key / empty map) resets fetcher and raises UpdateFailed."""
    coord = _map_coord(hass)

    fetcher = MagicMock()
    fetcher.fetch_all.return_value = None   # decode failure
    coord._fetcher = fetcher

    async def _exec(fn, *a):
        return fn(*a)

    with (
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
        pytest.raises(UpdateFailed, match="No valid map yet"),
    ):
        await coord._async_update_data()

    # Fetcher must be cleared so next cycle re-reads live key inputs.
    assert coord._fetcher is None


async def test_map_coordinator_stale_key_rebuild_on_next_cycle(
    hass: HomeAssistant,
) -> None:
    """After a None result clears the fetcher, the next call rebuilds it via _build."""
    coord = _map_coord(hass)

    fake_result = MagicMock()
    good_fetcher = MagicMock()
    good_fetcher.fetch_all.return_value = fake_result

    null_fetcher = MagicMock()
    null_fetcher.fetch_all.return_value = None

    coord._fetcher = null_fetcher

    async def _exec(fn, *a):
        return fn(*a)

    # First call: None → fetcher reset, UpdateFailed raised.
    with (
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
        pytest.raises(UpdateFailed),
    ):
        await coord._async_update_data()

    assert coord._fetcher is None

    # Second call: _build called (returns good fetcher), map succeeds.
    with (
        patch.object(coord, "_build", return_value=good_fetcher),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        result = await coord._async_update_data()

    assert result is fake_result


async def test_map_coordinator_no_fetcher_calls_build(
    hass: HomeAssistant,
) -> None:
    """On first update (no fetcher yet) _build is called to create one."""
    coord = _map_coord(hass)
    assert coord._fetcher is None

    fake_result = MagicMock()
    fetcher = MagicMock()
    fetcher.fetch_all.return_value = fake_result

    async def _exec(fn, *a):
        return fn(*a)

    with (
        patch.object(coord, "_build", return_value=fetcher),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        result = await coord._async_update_data()

    assert result is fake_result
    assert coord._fetcher is fetcher


async def test_map_coordinator_pass_token_persisted_after_refresh(
    hass: HomeAssistant,
) -> None:
    """_refresh_and_persist must write the new tokens back to the config entry."""
    coord = _map_coord(hass)

    cloud = MagicMock()
    cloud.service_token = "new_svc"
    cloud.ssecurity = "new_sec"
    cloud.pass_token = "new_pass"
    cloud.refresh.return_value = True
    coord._cloud = cloud

    async def _exec(fn, *a):
        return fn(*a)

    with (
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
        patch.object(hass.config_entries, "async_update_entry") as mock_update,
    ):
        ok = await coord._refresh_and_persist()

    assert ok is True
    mock_update.assert_called_once()
    updated_data = mock_update.call_args.kwargs["data"]
    assert updated_data[CONF_SERVICE_TOKEN] == "new_svc"
    assert updated_data[CONF_SSECURITY] == "new_sec"
    assert updated_data[CONF_PASS_TOKEN] == "new_pass"
