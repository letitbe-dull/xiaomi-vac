"""Cloud map coordinator: periodically fetch + parse the active map."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud.connector import XiaomiCloud
from .const import (
    CONF_DEVICE_ID,
    CONF_MAC,
    CONF_MODEL,
    CONF_PASS_TOKEN,
    CONF_SERVER,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
    CONF_WIFI_SN,
    DOMAIN,
    MAP_IDLE_INTERVAL,
    MAP_SCAN_INTERVAL,
)
from .coordinator import XiaomiVacuumCoordinator
from .device import IjaiVacuumDevice
from .map import MapFetcher, MapResult, SessionExpired
from .map_parsers import parser_key, required_map_key_inputs
from .spec.types import MapCapability

# activities where the map is actually changing
_ACTIVE = {"cleaning", "returning"}

_LOGGER = logging.getLogger(__name__)


class XiaomiMapCoordinator(DataUpdateCoordinator[MapResult]):
    """Holds a logged-in cloud session and refreshes the map."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: IjaiVacuumDevice,
        control: XiaomiVacuumCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:map:{entry.data[CONF_MODEL]}",
            update_interval=timedelta(seconds=MAP_IDLE_INTERVAL),
        )
        self.entry = entry
        self._device = device
        self._control = control
        self._cloud: XiaomiCloud | None = None
        self._fetcher: MapFetcher | None = None

    def _tune_interval(self) -> None:
        """Poll fast while the vacuum is moving, slowly when docked/idle."""
        data = self._control.data
        active = bool(data and data.activity in _ACTIVE)
        secs = MAP_SCAN_INTERVAL if active else MAP_IDLE_INTERVAL
        new = timedelta(seconds=secs)
        if self.update_interval != new:
            self.update_interval = new

    def _build(self) -> MapFetcher:
        """Blocking: restore the saved cloud session and construct a fetcher.

        wifi_sn and mac are read LIVE from the device (the source of truth for
        the map AES key); the values stashed at setup time can be empty/stale.
        """
        d = self.entry.data
        # No password: the saved session is restored and renewed via passToken.
        cloud = XiaomiCloud(d[CONF_USERNAME])
        cloud.restore_session(d[CONF_USER_ID], d[CONF_SSECURITY], d[CONF_SERVICE_TOKEN],
                              d.get(CONF_PASS_TOKEN))
        self._cloud = cloud

        brand = parser_key(self._device.profile)
        required = required_map_key_inputs(brand)

        cap = self._device.profile.map
        upload_action = None
        if (
            isinstance(cap, MapCapability)
            and cap.upload_by_mapid is not None
            and cap.upload_by_mapid.in_piid is not None
        ):
            upload_action = (cap.upload_by_mapid.siid, cap.upload_by_mapid.aiid)

        wifi_sn = ""
        mac = ""
        if required:
            live_sn = self._device.get_wifi_sn(d[CONF_USER_ID])
            live_mac = self._device.get_mac()
            wifi_sn = live_sn or d.get(CONF_WIFI_SN) or ""
            mac = live_mac or d.get(CONF_MAC) or ""

        _LOGGER.debug(
            "Map key inputs: brand=%s wifi_sn set=%s mac=%s user_id=%s device_id=%s model=%s",
            brand, bool(wifi_sn), mac, d[CONF_USER_ID], d[CONF_DEVICE_ID], d[CONF_MODEL],
        )
        if "wifi_sn" in required and not wifi_sn:
            raise UpdateFailed("Could not read wifi_sn from the vacuum (needed to decrypt map)")
        if "device_mac" in required and not mac:
            # An empty mac silently produces the wrong AES key (decrypt fails
            # with "Padding is incorrect"), so refuse to build with one.
            raise UpdateFailed("Could not read mac from the vacuum (needed to decrypt map)")

        return MapFetcher(
            cloud,
            server=d[CONF_SERVER],
            user_id=d[CONF_USER_ID],
            device_id=d[CONF_DEVICE_ID],
            model=d[CONF_MODEL],
            mac=mac,
            wifi_sn=wifi_sn,
            parser_brand=brand,
            upload_action=upload_action,
        )

    async def _async_update_data(self) -> MapResult:
        self._tune_interval()
        try:
            if self._fetcher is None:
                self._fetcher = await self.hass.async_add_executor_job(self._build)
            # The map list (distinct physical maps) drives multi-map; best-effort
            # so a flaky local read never blocks the active-map fetch.
            try:
                maps_meta = await self.hass.async_add_executor_job(self._device.map_list)
            except Exception:  # noqa: BLE001
                maps_meta = []
            try:
                result = await self.hass.async_add_executor_job(
                    self._fetcher.fetch_all, maps_meta)
            except SessionExpired:
                # Token likely expired — renew it with the passToken and retry.
                # If renewal fails, the passToken is dead too: ask the user to
                # re-auth (raises a reauth flow) rather than dead-end.
                if not await self._refresh_and_persist():
                    raise ConfigEntryAuthFailed("Cloud session expired") from None
                result = await self.hass.async_add_executor_job(
                    self._fetcher.fetch_all, maps_meta)
            if result is None:
                # A None result means decode failed (handled in fetch). This is
                # usually a transient corrupt/empty map, but it's ALSO what a
                # fetcher built with stale key inputs produces — e.g. wifi_sn/mac
                # read while the device was briefly unreachable at startup. Drop
                # the fetcher so the next cycle re-reads live values and self-heals
                # once the device is back, instead of staying poisoned forever.
                self._fetcher = None
                raise UpdateFailed("No valid map yet (corrupt or empty map)")
            return result
        except (UpdateFailed, ConfigEntryAuthFailed):
            raise
        except SessionExpired:
            raise ConfigEntryAuthFailed("Cloud session expired") from None
        except Exception as err:  # noqa: BLE001
            self._fetcher = None
            raise UpdateFailed(f"Map update error: {err}") from err

    async def _refresh_and_persist(self) -> bool:
        """Renew the cloud session via passToken and save the new tokens."""
        if self._cloud is None:
            return False
        if not await self.hass.async_add_executor_job(self._cloud.refresh):
            return False
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_SERVICE_TOKEN: self._cloud.service_token,
                CONF_SSECURITY: self._cloud.ssecurity,
                CONF_PASS_TOKEN: self._cloud.pass_token or "",
            },
        )
        _LOGGER.info("Renewed Xiaomi cloud session via passToken")
        return True
