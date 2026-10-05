"""Harness tests for the config-entry diagnostics download."""
from __future__ import annotations

import json
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_vac.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MODEL,
    CONF_OAUTH_ACCESS_TOKEN,
    CONF_OAUTH_DEVICE_ID,
    CONF_OAUTH_REDIRECT_URI,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_PASS_TOKEN,
    CONF_SERVER,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.xiaomi_vac.spec.registry import get_profile

MODEL = "ijai.vacuum.v17"

ENTRY_SECRETS = {
    CONF_HOST: "192.168.77.41",
    CONF_TOKEN: "d7e1a0c4b9f24e6a8c3b5d7f9e1a2c4b",
    CONF_USERNAME: "reporter-sentinel@example.invalid",
    CONF_USER_ID: "7730018842",
    CONF_DEVICE_ID: "5519377402",
    CONF_SSECURITY: "SSECURITY-sentinel-5q2",
    CONF_SERVICE_TOKEN: "SERVICETOKEN-sentinel-9x7",
    CONF_PASS_TOKEN: "PASSTOKEN-sentinel-3k1",
    CONF_OAUTH_ACCESS_TOKEN: "OAUTHACCESS-sentinel-7m4",
    CONF_OAUTH_REFRESH_TOKEN: "OAUTHREFRESH-sentinel-2w8",
    CONF_OAUTH_DEVICE_ID: "ha.OAUTHDEVICE-sentinel",
    CONF_OAUTH_REDIRECT_URI: "https://ha-sentinel.example.invalid/callback",
}
LIVE_MAC = "5C:E5:0C:9A:3B:71"
LIVE_WIFI_SN = "WIFISN-sentinel-0042"
SIGNED_URL = (
    "https://awsde0.fds.api.xiaomi.com/robomap/obj"
    "?GalaxyAccessKeyId=AKID-sentinel&Expires=1790000000&Signature=SIG-sentinel"
)
BLOB = b"not-a-map-blob"

SECRETS = [
    *ENTRY_SECRETS.values(), LIVE_MAC, LIVE_WIFI_SN, SIGNED_URL, "AKID-sentinel", "SIG-sentinel",
]


@pytest.fixture
async def loaded_entry(hass: HomeAssistant):
    """A loaded map-capable entry whose one map cycle got a URL and an undecryptable blob.

    @returns: (entry, fake cloud session, fake device), boundary patches held open.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, unique_id="diagnostics-test",
        data={CONF_MODEL: MODEL, CONF_SERVER: "de", **ENTRY_SECRETS},
    )
    entry.add_to_hass(hass)

    device = MagicMock()
    device.model = MODEL
    device.profile = get_profile(MODEL)
    device.status.return_value = SimpleNamespace(activity="docked")
    device.get_wifi_sn.return_value = LIVE_WIFI_SN
    device.get_mac.return_value = LIVE_MAC
    device.map_list.return_value = [{"name": "Ground floor", "id": 7, "cur": True}]

    with (
        patch("custom_components.xiaomi_vac.IjaiVacuumDevice", return_value=device),
        patch("custom_components.xiaomi_vac.map_coordinator.XiaomiCloud") as cloud_cls,
        patch(
            "custom_components.xiaomi_vac.async_refresh_oauth_entry",
            new=AsyncMock(return_value=False),
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", new=AsyncMock(return_value=True),
        ),
    ):
        cloud = cloud_cls.return_value
        cloud.map_url.return_value = SIGNED_URL
        cloud.download.return_value = BLOB
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        yield entry, cloud, device


async def _download(hass: HomeAssistant, hass_client, entry: MockConfigEntry):
    """Download config-entry diagnostics over HTTP as the user would.

    @returns: the raw response body text.
    """
    assert await async_setup_component(hass, "diagnostics", {})
    await hass.async_block_till_done()
    client = await hass_client()
    response = await client.get(f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert response.status == HTTPStatus.OK
    return await response.text()


async def test_diagnostics_download_returns_last_map_cycle(
    hass: HomeAssistant, hass_client, loaded_entry,
) -> None:
    """The download carries what the last map cycle did."""
    entry, _, _ = loaded_entry

    cycle = json.loads(await _download(hass, hass_client, entry))["data"]["map_cycle"]

    assert cycle["map_capability"]["service"] == 10
    assert {k: v for k, v in cycle.items() if k != "map_capability"} == {
        "parser_key": "ijai",
        "map_key_owner_source": "user_id",
        "map_key_owner_matches_user_id": None,
        "url_obtained": True,
        "session_expired": False,
        "rendered": False,
        "slots": [
            {"slot": "0", "url_obtained": True, "blob_bytes": 14, "outcome": "undecryptable"},
            {"slot": "1", "url_obtained": True, "blob_bytes": 14, "outcome": "undecryptable"},
        ],
        "resolved_map_id": 7,
        "resolved_by": "map_list",
        "served": "none",
        "upload_request_sent": False,
        "upload_request_route": None,
        "upload_request_ok": None,
    }


async def test_diagnostics_download_contains_no_secret(
    hass: HomeAssistant, hass_client, loaded_entry,
) -> None:
    """No token, credential, user id, MAC, local IP or signed URL is in the downloaded file."""
    entry, _, _ = loaded_entry

    body = (await _download(hass, hass_client, entry)).lower()

    assert [s for s in SECRETS if s.lower() in body] == []


async def test_diagnostics_download_makes_no_cloud_or_device_call(
    hass: HomeAssistant, hass_client, loaded_entry,
) -> None:
    """Downloading diagnostics reports the last cycle without fetching a new one."""
    entry, cloud, device = loaded_entry
    cloud.reset_mock()
    device.reset_mock()

    await _download(hass, hass_client, entry)

    assert cloud.mock_calls == []
    assert device.mock_calls == []
