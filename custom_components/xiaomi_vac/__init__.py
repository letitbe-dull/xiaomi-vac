"""The Xiaomi Vacuum integration (local MIoT control + cloud map)."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .cloud.mqtt import MiotMqttClient, MqttMessage
from .cloud.oauth import async_refresh_oauth_entry
from .const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MODEL,
    CONF_OAUTH_ACCESS_TOKEN,
    CONF_OAUTH_DEVICE_ID,
    CONF_OAUTH_REGION,
    CONF_PASSWORD,
    CONF_SERVICE_TOKEN,
    CONF_TOKEN,
    DOMAIN,
)
from .coordinator import XiaomiVacuumCoordinator
from .device import IjaiVacuumDevice
from .map_coordinator import XiaomiMapCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.CAMERA,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.NUMBER,
]


@dataclass
class XiaomiVacuumData:
    """Runtime data stored on the config entry (see runtime-data rule)."""

    control: XiaomiVacuumCoordinator
    map: XiaomiMapCoordinator | None
    mqtt: MiotMqttClient | None = None


type XiaomiConfigEntry = ConfigEntry[XiaomiVacuumData]


_CARD_BASE = "/xiaomi-vac-card"
_CARD_URL = f"{_CARD_BASE}/xiaomi-vac-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve and auto-register the Lovelace card (no manual resource setup)."""
    hass.data.setdefault(DOMAIN, {})

    from .http_api import VectorMapView

    hass.http.register_view(VectorMapView())

    www = os.path.join(os.path.dirname(__file__), "www")
    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(_CARD_BASE, www, False)]
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("xiaomi_vac: failed to serve card files (%s)", err)
        return True

    # Register the card as a Lovelace RESOURCE (proven reliable), not via
    # add_extra_js_url (which intermittently fails to load the module). Done once
    # HA has started so the Lovelace resource collection exists.
    from homeassistant.helpers import start as ha_start

    async def _register_card_resource(_event=None) -> None:
        from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE

        ll = hass.data.get(LOVELACE_DATA)
        if ll is None:
            _LOGGER.warning("xiaomi_vac: Lovelace not ready; add %s manually", _CARD_URL)
            return
        if ll.resource_mode != MODE_STORAGE:
            _LOGGER.warning(
                "xiaomi_vac: Lovelace is in YAML mode; add this resource yourself:"
                " url: %s, type: module", _CARD_URL)
            return

        # Cache-bust by file mtime so card updates are fetched fresh (HACS does
        # the same with ?hacstag=). Keep a single resource, update it in place.
        try:
            ver = int(os.path.getmtime(os.path.join(www, "xiaomi-vac-card.js")))
        except OSError:
            ver = 0
        url = f"{_CARD_URL}?v={ver}"

        resources = ll.resources
        await resources.async_get_info()  # ensure loaded
        existing = [r for r in resources.async_items()
                    if r.get("url", "").split("?")[0] == _CARD_URL]
        if existing:
            if existing[0].get("url") != url:
                await resources.async_update_item(existing[0]["id"], {"url": url})
                _LOGGER.info("xiaomi_vac: updated card resource -> %s", url)
        else:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("xiaomi_vac: registered card resource -> %s", url)

    ha_start.async_at_started(hass, _register_card_resource)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: XiaomiConfigEntry) -> bool:
    """Set up Xiaomi Vacuum from a config entry."""
    data = entry.data

    device = await hass.async_add_executor_job(
        IjaiVacuumDevice, data[CONF_HOST], data[CONF_TOKEN], data[CONF_MODEL]
    )
    control = XiaomiVacuumCoordinator(hass, entry, device)
    await control.async_config_entry_first_refresh()

    map_coordinator: XiaomiMapCoordinator | None = None
    # Map is optional: only if a cloud session was captured at setup time.
    if data.get(CONF_SERVICE_TOKEN):
        map_coordinator = XiaomiMapCoordinator(hass, entry, device, control)
        # don't fail the whole entry if the first map fetch hiccups
        await map_coordinator.async_refresh()

    mqtt_client = await _async_start_mqtt(hass, entry, map_coordinator, control)

    entry.runtime_data = XiaomiVacuumData(
        control=control, map=map_coordinator, mqtt=mqtt_client,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: XiaomiConfigEntry) -> bool:
    """Unload a config entry."""
    data = entry.runtime_data
    if data is not None and data.mqtt is not None:
        await data.mqtt.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_start_mqtt(
    hass: HomeAssistant,
    entry: XiaomiConfigEntry,
    map_coord: XiaomiMapCoordinator | None,
    control_coord: XiaomiVacuumCoordinator,
) -> MiotMqttClient | None:
    """Bring up the MIoT cloud MQTT client for this entry when OAuth is set.

    Additive & optional per the plan: any missing OAuth field → no client, no
    error, existing behaviour intact.
    """
    data = entry.data
    required = (
        data.get(CONF_OAUTH_ACCESS_TOKEN),
        data.get(CONF_OAUTH_REGION),
        data.get(CONF_OAUTH_DEVICE_ID),
        data.get(CONF_DEVICE_ID),
    )
    if not all(required):
        return None

    async def _token_provider(force: bool) -> str:
        if force:
            await async_refresh_oauth_entry(hass, entry, force=True)
        return str(entry.data.get(CONF_OAUTH_ACCESS_TOKEN, ""))

    async def _on_message(message: MqttMessage) -> None:
        if map_coord is not None:
            await map_coord.async_on_mqtt_message(message)
        # Optional: vacuum status (siid 2 / piid 1) for faster control coordinator updates
        if message.kind == "property" and message.siid == 2 and message.piid == 1:
            hass.async_create_task(control_coord.async_request_refresh())

    client = MiotMqttClient(
        hass,
        region=str(data[CONF_OAUTH_REGION]),
        did=str(data[CONF_DEVICE_ID]),
        client_device_id=str(data[CONF_OAUTH_DEVICE_ID]),
        access_token=str(data[CONF_OAUTH_ACCESS_TOKEN]),
        token_provider=_token_provider,
        on_message=_on_message,
    )
    try:
        await client.async_start()
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Failed to start MIoT MQTT client — continuing without it")
        return None
    return client


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries forward.

    Bump ConfigFlow.VERSION and add a branch here when entry.data changes shape,
    so existing users never have to log in again. New optional keys that the code
    reads with .get() don't need a migration at all.
    """
    if entry.version == 1:
        # v1 -> v2: stop persisting the Mi account password. The session is kept
        # alive with the long-lived passToken, so the stored password is dead
        # weight; drop it. (Local-only entries never had one.)
        data = dict(entry.data)
        data.pop(CONF_PASSWORD, None)
        hass.config_entries.async_update_entry(entry, data=data, version=2)
        return True
    return False
