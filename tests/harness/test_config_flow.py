"""Tests for the Xiaomi Vacuum config flow."""
import asyncio
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_vac import async_migrate_entry
from custom_components.xiaomi_vac.cloud.connector import CloudUnreachable
from custom_components.xiaomi_vac.captcha_view import ImageView
from custom_components.xiaomi_vac.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MODEL,
    CONF_OAUTH_ACCESS_TOKEN,
    CONF_OAUTH_DEVICE_ID,
    CONF_OAUTH_EXPIRES_TS,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_OAUTH_REGION,
    CONF_OAUTH_REDIRECT_URI,
    CONF_OWNER_UID,
    CONF_PASS_TOKEN,
    CONF_PASSWORD,
    CONF_SERVER,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_USERNAME,
    CONF_WIFI_SN,
    DOMAIN,
)

TOKEN = "0" * 32
OWNER_UID = "7777777777"
TRANSLATIONS_EN = (
    Path(__file__).resolve().parents[2]
    / "custom_components"
    / "xiaomi_vac"
    / "translations"
    / "en.json"
)

@pytest.fixture(autouse=True)
def mock_setup_entry():
    """Keep config-flow tests from exercising live device setup."""
    with patch("custom_components.xiaomi_vac.async_setup_entry", return_value=True):
        yield


async def _open_local_form(hass: HomeAssistant) -> dict:
    """Walk user menu -> local step, return the local form result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "local"}
    )


async def test_user_step_shows_menu(hass: HomeAssistant) -> None:
    """The entry step offers the cloud/local choice."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"credentials", "local"}


async def test_local_step_success(hass: HomeAssistant) -> None:
    """A reachable, supported device creates an entry."""
    form = await _open_local_form(hass)
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "local"

    with patch(
        "custom_components.xiaomi_vac.config_flow._probe",
        return_value={"model": "ijai.vacuum.v3", "mac": "AA:BB:CC:DD:EE:FF"},
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "ijai.vacuum.v3"
    assert result["data"][CONF_HOST] == "1.2.3.4"
    assert result["data"][CONF_MODEL] == "ijai.vacuum.v3"


async def test_local_step_dreame_supported(hass: HomeAssistant) -> None:
    """The registry gate accepts a dreame model the old prefix list rejected."""
    form = await _open_local_form(hass)

    with patch(
        "custom_components.xiaomi_vac.config_flow._probe",
        return_value={"model": "dreame.vacuum.p2008", "mac": "AA:BB:CC:DD:EE:01"},
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "dreame.vacuum.p2008"


async def test_local_step_rich_reference_unsupported(hass: HomeAssistant) -> None:
    """A profile without a runnable core (roidmi) is rejected at onboarding."""
    form = await _open_local_form(hass)

    with patch(
        "custom_components.xiaomi_vac.config_flow._probe",
        return_value={"model": "roidmi.vacuum.r1b", "mac": ""},
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_model"}


async def test_local_step_cannot_connect(hass: HomeAssistant) -> None:
    """A probe failure surfaces as a cannot_connect error on the form."""
    form = await _open_local_form(hass)

    with patch(
        "custom_components.xiaomi_vac.config_flow._probe",
        side_effect=Exception("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_local_step_unsupported_model(hass: HomeAssistant) -> None:
    """A reachable but unsupported device is rejected on the form."""
    form = await _open_local_form(hass)

    with patch(
        "custom_components.xiaomi_vac.config_flow._probe",
        return_value={"model": "roborock.vacuum.a01", "mac": ""},
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_model"}


# ---------------------------------------------------------------------------
# Cloud credentials flow
# ---------------------------------------------------------------------------

def _make_device(model: str, did: str = "d1") -> dict:
    suffix = did[-2:].zfill(2)
    return {
        "name": model, "did": did, "model": model,
        "mac": f"AA:BB:CC:DD:EE:{suffix}", "localip": "1.2.3.4",
        "token": TOKEN, "server": "cn", "uid": OWNER_UID, "owner_uid": OWNER_UID,
    }


_NO_TRANSPORT = object()


def _cloud_patches(
    login_state: str = "ok",
    devices: list | None = None,
    transport: object = _NO_TRANSPORT,
):
    """Context-manager stack: stub login, device list, and wifi_sn fetch.

    With ``transport`` set, list_vacuums runs for real and every regional
    request returns ``transport``, or its result if callable (None = server did not answer).
    """
    if devices is None:
        devices = []
    if transport is _NO_TRANSPORT:
        discovery = patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.list_vacuums",
            return_value=devices,
        )
    else:
        discovery = patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud._call",
            return_value=transport,
            side_effect=transport if callable(transport) else None,
        )
    return [
        patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
            return_value=login_state,
        ),
        discovery,
        patch(
            "custom_components.xiaomi_vac.config_flow.IjaiVacuumDevice.get_wifi_sn",
            return_value=None,
        ),
    ]


async def _credentials_to_devices(
    hass: HomeAssistant,
    devices: list,
    *,
    stop_at_oauth: bool = False,
    transport: object = _NO_TRANSPORT,
) -> dict:
    """Open the credentials form and submit it; return the next flow result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )
    assert result["step_id"] == "credentials"

    with ExitStack() as stack:
        for p in _cloud_patches(devices=devices, transport=transport):
            stack.enter_context(p)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )
    if (
        stop_at_oauth
        or result["type"] is not FlowResultType.FORM
        or result.get("step_id") != "miot_oauth"
    ):
        return result
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_miot_oauth": False}
    )


async def test_cloud_dreame_supported_creates_entry(hass: HomeAssistant) -> None:
    """A dreame model that passes is_supported() creates an entry via cloud flow."""
    result = await _credentials_to_devices(hass, [_make_device("dreame.vacuum.p2008")])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "dreame.vacuum.p2008"


async def test_cloud_entry_does_not_store_password(hass: HomeAssistant) -> None:
    """Phase 5: a cloud setup must persist tokens but never the password."""
    result = await _credentials_to_devices(hass, [_make_device("dreame.vacuum.p2008")])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_PASSWORD not in result["data"]
    assert result["data"][CONF_USERNAME] == "user@example.com"
    # Session-token keys must still be present (so renewal works without password).
    assert CONF_SERVICE_TOKEN in result["data"]


async def test_cloud_oauth_skip_keeps_legacy_entry_data(hass: HomeAssistant) -> None:
    """Skipping optional MIoT OAuth creates the current legacy cloud entry."""
    result = await _credentials_to_devices(
        hass, [_make_device("dreame.vacuum.p2008")], stop_at_oauth=True
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "miot_oauth"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_miot_oauth": False}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert CONF_OAUTH_ACCESS_TOKEN not in result["data"]
    assert CONF_SERVICE_TOKEN in result["data"]


async def test_cloud_oauth_success_stores_miot_tokens(hass: HomeAssistant) -> None:
    """Opting into MIoT OAuth links through HA's webhook progress flow."""
    result = await _credentials_to_devices(
        hass, [_make_device("dreame.vacuum.p2008")], stop_at_oauth=True
    )
    updates = {
        CONF_OAUTH_ACCESS_TOKEN: "access",
        CONF_OAUTH_REFRESH_TOKEN: "refresh",
        CONF_OAUTH_EXPIRES_TS: 1234,
        CONF_OAUTH_REGION: "sg",
        CONF_OAUTH_DEVICE_ID: "ha.webhook",
        CONF_OAUTH_REDIRECT_URI: "http://homeassistant.local:8123/api/webhook/abc",
    }
    async def _slow_exchange(*args, **kwargs):
        # Yield once so the eagerly-started task is still pending when the
        # flow renders the progress step (the real exchange awaits a webhook).
        await asyncio.sleep(0)
        return updates

    with patch(
        "custom_components.xiaomi_vac.config_flow._async_exchange_linked_oauth",
        side_effect=_slow_exchange,
    ), patch(
        "custom_components.xiaomi_vac.config_flow.browser_on_oauth_redirect",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enable_miot_oauth": True}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        authorize_url = result["description_placeholders"]["authorize_url"]
        assert "account.xiaomi.com/oauth2/authorize" in authorize_url
        # redirect_uri is percent-encoded inside the authorize URL
        assert "%2Fapi%2Fwebhook%2F" in authorize_url

        # HA auto-advances through show_progress_done once the task finishes
        await asyncio.sleep(0)
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OAUTH_ACCESS_TOKEN] == "access"
    assert result["data"][CONF_OAUTH_REFRESH_TOKEN] == "refresh"
    assert result["data"][CONF_OAUTH_EXPIRES_TS] == 1234
    assert result["data"][CONF_OAUTH_REGION] == "sg"
    assert result["data"][CONF_OAUTH_DEVICE_ID] == "ha.webhook"
    assert result["data"][CONF_OAUTH_REDIRECT_URI].endswith("/api/webhook/abc")


async def test_cloud_oauth_paste_redirect_url_stores_miot_tokens(
    hass: HomeAssistant,
) -> None:
    """Off homeassistant.local, the menu's paste option takes the redirect URL."""
    result = await _credentials_to_devices(
        hass, [_make_device("dreame.vacuum.p2008")], stop_at_oauth=True
    )
    with patch(
        "custom_components.xiaomi_vac.config_flow.browser_on_oauth_redirect",
        return_value=False,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enable_miot_oauth": True}
        )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "miot_oauth_method"
    assert set(result["menu_options"]) == {"miot_oauth_auth", "miot_oauth_code"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "miot_oauth_code"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "miot_oauth_code"
    assert not result["errors"]

    captured = {}

    async def _exchange(hass_, code, data, device_id):
        captured["code"] = code
        return {CONF_OAUTH_ACCESS_TOKEN: "access", CONF_OAUTH_DEVICE_ID: device_id}

    with patch(
        "custom_components.xiaomi_vac.config_flow._async_exchange_manual_oauth",
        side_effect=_exchange,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"code": " http://homeassistant.local:8123/?code=ALSG_abc&state=xyz "},
        )

    assert captured["code"] == "ALSG_abc"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_OAUTH_ACCESS_TOKEN] == "access"


async def test_cloud_oauth_menu_automatic_shows_progress(hass: HomeAssistant) -> None:
    """Off homeassistant.local, the menu's automatic option still links via webhook."""
    result = await _credentials_to_devices(
        hass, [_make_device("dreame.vacuum.p2008")], stop_at_oauth=True
    )
    with patch(
        "custom_components.xiaomi_vac.config_flow.browser_on_oauth_redirect",
        return_value=False,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"enable_miot_oauth": True}
        )
    assert result["type"] is FlowResultType.MENU

    async def _pending(*args, **kwargs):
        await asyncio.Event().wait()

    with patch(
        "custom_components.xiaomi_vac.config_flow._async_exchange_linked_oauth",
        side_effect=_pending,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "miot_oauth_auth"}
        )
        assert result["type"] is FlowResultType.SHOW_PROGRESS
        assert "%2Fapi%2Fwebhook%2F" in result["description_placeholders"]["authorize_url"]
        hass.config_entries.flow.async_abort(result["flow_id"])


async def test_options_oauth_menu_paste_stores_miot_tokens(hass: HomeAssistant) -> None:
    """The options flow takes the same menu route off homeassistant.local."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_USERNAME: "user@example.com", CONF_SERVER: "de"},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.xiaomi_vac.config_flow.browser_on_oauth_redirect",
        return_value=False,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "miot_oauth_method"
    assert set(result["menu_options"]) == {"miot_oauth_auth", "miot_oauth_code"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "miot_oauth_code"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "miot_oauth_code"

    with patch(
        "custom_components.xiaomi_vac.config_flow._async_exchange_manual_oauth",
        return_value={CONF_OAUTH_ACCESS_TOKEN: "access"},
    ):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"code": "http://homeassistant.local:8123/?code=ALDE_abc&state=xyz"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_OAUTH_ACCESS_TOKEN] == "access"


def test_generated_english_translation_contains_oauth_config_step() -> None:
    """translations/en.json must include the config-flow OAuth link step."""
    doc = json.loads(TRANSLATIONS_EN.read_text(encoding="utf-8"))

    step = doc["config"]["step"]["miot_oauth_code"]

    assert "{authorize_url}" in step["description"]
    assert step["data"]["code"] == "OAuth code"
    assert "{authorize_url}" in doc["config"]["progress"]["miot_oauth_auth"]


async def test_reauth_mints_tokens_and_discards_password(hass: HomeAssistant) -> None:
    """Phase 5: reauth swaps in fresh tokens and leaves no password behind."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_MODEL: "dreame.vacuum.p2008",
            CONF_USER_ID: "old",
            CONF_SSECURITY: "old",
            CONF_SERVICE_TOKEN: "old",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    def _set_session(self):
        self.user_id = "new"
        self.ssecurity = "newsec"
        self.service_token = "newtoken"
        self.pass_token = "newpass"
        return "ok"

    with patch(
        "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
        autospec=True,
        side_effect=_set_session,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_SERVICE_TOKEN] == "newtoken"
    assert entry.data[CONF_SSECURITY] == "newsec"
    assert entry.data[CONF_PASS_TOKEN] == "newpass"
    assert entry.data[CONF_USER_ID] == "new"


async def test_migration_strips_password(hass: HomeAssistant) -> None:
    """Phase 5: migrating a v1 entry drops CONF_PASSWORD and bumps the version."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_SERVICE_TOKEN: "tok",
        },
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert CONF_PASSWORD not in entry.data
    assert entry.data[CONF_SERVICE_TOKEN] == "tok"


async def test_cloud_viomi_supported_creates_entry(hass: HomeAssistant) -> None:
    """A viomi model that passes is_supported() creates an entry via cloud flow."""
    result = await _credentials_to_devices(hass, [_make_device("viomi.vacuum.v12", did="d2")])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "viomi.vacuum.v12"


async def test_cloud_roidmi_rejected(hass: HomeAssistant) -> None:
    """A roidmi model (rich-reference only, no core) aborts with unsupported_model."""
    result = await _credentials_to_devices(hass, [_make_device("roidmi.vacuum.r1b")])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_model"


async def test_cloud_unknown_model_rejected(hass: HomeAssistant) -> None:
    """An unknown vacuum model aborts with unsupported_model."""
    result = await _credentials_to_devices(hass, [_make_device("unknown.vacuum.x99")])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_model"
    assert result["description_placeholders"] == {"model": "unknown.vacuum.x99"}


async def test_cloud_no_vacuums_aborts(hass: HomeAssistant) -> None:
    """An account with no vacuum devices aborts with no_devices."""
    result = await _credentials_to_devices(hass, [])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


def _answer(devices: list) -> dict:
    return {"code": 0, "result": {"list": devices}}


async def test_cloud_no_server_answered_aborts_no_server_response(
    hass: HomeAssistant,
) -> None:
    """Every regional call returning nothing aborts with no_server_response."""
    result = await _credentials_to_devices(hass, [], transport=None)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_server_response"


async def test_cloud_servers_answered_empty_aborts_no_devices(
    hass: HomeAssistant,
) -> None:
    """Servers answering with zero devices abort with no_devices."""
    result = await _credentials_to_devices(hass, [], transport=_answer([]))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


async def test_cloud_servers_answered_only_non_vacuums_aborts_no_devices(
    hass: HomeAssistant,
) -> None:
    """Servers answering with only non-vacuum devices abort with no_devices."""
    result = await _credentials_to_devices(
        hass, [], transport=_answer([_make_device("yeelink.light.x")])
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


async def test_cloud_servers_answered_unsupported_vacuum_aborts_unsupported_model(
    hass: HomeAssistant,
) -> None:
    """Servers answering with a vacuum the registry rejects abort with unsupported_model."""
    result = await _credentials_to_devices(
        hass, [], transport=_answer([_make_device("roidmi.vacuum.r1b")])
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_model"


async def test_cloud_several_unsupported_vacuums_abort_unsupported_model(
    hass: HomeAssistant,
) -> None:
    """Several unsupported brands together still abort with unsupported_model, not no_devices."""
    result = await _credentials_to_devices(
        hass,
        [],
        transport=_answer(
            [
                _make_device("roidmi.vacuum.r1b", did="d1"),
                _make_device("unknown.vacuum.x99", did="d2"),
            ]
        ),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_model"
    assert result["description_placeholders"] == {
        "model": "roidmi.vacuum.r1b, unknown.vacuum.x99"
    }


async def test_cloud_single_supported_via_real_discovery_skips_picker(
    hass: HomeAssistant,
) -> None:
    """One supported device among unsupported ones is set up without the picker."""
    result = await _credentials_to_devices(
        hass,
        [],
        transport=_answer(
            [
                _make_device("dreame.vacuum.p2008", did="d1"),
                _make_device("roidmi.vacuum.r1b", did="d2"),
            ]
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "dreame.vacuum.p2008"
    assert result["data"][CONF_OWNER_UID] == OWNER_UID


HOME_OWNER_UID = "5555555555"


def _shared_home_only(url: str, params: dict) -> dict:
    """Fake `_call`: flat lists are empty; one shared home holds a supported vacuum."""
    if url.endswith("/v2/homeroom/gethome"):
        return {"code": 0, "result": {
            "homelist": [], "share_home_list": [{"id": 77, "uid": int(HOME_OWNER_UID)}],
        }}
    if url.endswith("/v2/home/home_device_list"):
        return {"code": 0, "result": {"device_info": [_make_device("dreame.vacuum.p2008")]}}
    return _answer([])


async def test_cloud_vacuum_only_in_shared_home_creates_entry_with_home_owner_uid(
    hass: HomeAssistant,
) -> None:
    """A vacuum found only through a shared home is set up with the home owner's uid."""
    result = await _credentials_to_devices(hass, [], transport=_shared_home_only)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "dreame.vacuum.p2008"
    assert result["data"][CONF_OWNER_UID] == HOME_OWNER_UID


async def test_cloud_mixed_account_shows_only_supported(hass: HomeAssistant) -> None:
    """Mixed account: unsupported models are filtered out; only supported one is set up."""
    supported = _make_device("dreame.vacuum.p2008", did="d1")
    unsupported = _make_device("roidmi.vacuum.r1b", did="d2")

    # Single supported device → auto-selected without showing the picker form.
    result = await _credentials_to_devices(hass, [supported, unsupported])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "dreame.vacuum.p2008"


async def test_cloud_login_failed_aborts(hass: HomeAssistant) -> None:
    """A non-ok, non-captcha, non-2fa state from begin_login aborts the flow."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )
    with ExitStack() as stack:
        for p in _cloud_patches(login_state="error"):
            stack.enter_context(p)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "login_failed"


def _wrong_password(self):
    self.login_error = "登录验证失败"
    self.login_code = 70016
    return "fail"


async def test_cloud_wrong_password_reshows_form(hass: HomeAssistant) -> None:
    """Xiaomi's 70016 reply re-shows the credentials form with invalid_auth."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )
    with patch(
        "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
        autospec=True,
        side_effect=_wrong_password,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "wrong"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reauth_wrong_password_reshows_form(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="AA:BB:CC:DD:EE:FF",
        data={CONF_USERNAME: "user@example.com", CONF_MODEL: "dreame.vacuum.p2008"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
        autospec=True,
        side_effect=_wrong_password,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "wrong"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cloud_captcha_step_shown_when_required(hass: HomeAssistant) -> None:
    """begin_login returning 'captcha' must route to the captcha step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )
    assert result["step_id"] == "credentials"

    with (
        patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
            return_value="captcha",
        ),
        patch(
            "custom_components.xiaomi_vac.captcha_view.ensure_registered",
        ),
        patch(
            "custom_components.xiaomi_vac.captcha_view.set_image",
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "captcha"


async def test_cloud_captcha_submit_continues_to_entry(hass: HomeAssistant) -> None:
    """Submitting a correct captcha code proceeds to device discovery and entry creation."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )

    with (
        patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
            return_value="captcha",
        ),
        patch("custom_components.xiaomi_vac.captcha_view.ensure_registered"),
        patch("custom_components.xiaomi_vac.captcha_view.set_image"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )
    assert result["step_id"] == "captcha"

    with ExitStack() as stack:
        stack.enter_context(patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.submit_captcha",
            return_value="ok",
        ))
        for p in _cloud_patches(login_state="ok", devices=[_make_device("dreame.vacuum.p2008")]):
            # Only the list_vacuums and wifi_sn patches are needed here.
            stack.enter_context(p)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "ab12"}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_miot_oauth": False}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "dreame.vacuum.p2008"


async def _flow_at_captcha(hass: HomeAssistant, png: bytes) -> dict:
    """Drive a fresh flow to the captcha step with the cloud serving `png`."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )

    def _begin_login(cloud) -> str:
        cloud.captcha_image = png
        return "captcha"

    with (
        patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
            autospec=True,
            side_effect=_begin_login,
        ),
        patch("custom_components.xiaomi_vac.config_flow.ensure_registered"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )
    assert result["step_id"] == "captcha"
    return result


async def _served_image(hass: HomeAssistant, result: dict):
    """Fetch the captcha image the way the dialog does, from the URL in the form."""
    url = result["description_placeholders"]["captcha_url"]
    query = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    request = SimpleNamespace(app={"hass": hass}, query=query)
    return await ImageView().get(request)


async def test_concurrent_flows_each_serve_their_own_captcha(
    hass: HomeAssistant,
) -> None:
    """Two flows at the captcha step must not see each other's image."""
    first = await _flow_at_captcha(hass, b"png-of-flow-one")
    second = await _flow_at_captcha(hass, b"png-of-flow-two")

    assert (await _served_image(hass, first)).body == b"png-of-flow-one"
    assert (await _served_image(hass, second)).body == b"png-of-flow-two"


async def test_captcha_image_is_gone_once_flow_ends(hass: HomeAssistant) -> None:
    """Aborting a flow at the captcha step must stop serving its image."""
    result = await _flow_at_captcha(hass, b"png-of-flow-one")
    assert (await _served_image(hass, result)).status == 200

    hass.config_entries.flow.async_abort(result["flow_id"])

    assert (await _served_image(hass, result)).status == 404


async def test_cloud_twofa_step_shown_when_required(hass: HomeAssistant) -> None:
    """begin_login returning '2fa' must route to the twofa step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )

    with patch(
        "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
        return_value="2fa",
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "twofa"


async def test_cloud_twofa_submit_continues_to_entry(hass: HomeAssistant) -> None:
    """Submitting the 2FA code proceeds to device discovery and entry creation."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )

    with patch(
        "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
        return_value="2fa",
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_USERNAME: "user@example.com", CONF_PASSWORD: "secret"},
        )
    assert result["step_id"] == "twofa"

    with ExitStack() as stack:
        stack.enter_context(patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.submit_2fa",
            return_value="ok",
        ))
        for p in _cloud_patches(devices=[_make_device("dreame.vacuum.p2008")]):
            stack.enter_context(p)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456"}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_miot_oauth": False}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "dreame.vacuum.p2008"


async def test_cloud_device_picker_shown_with_multiple_supported(hass: HomeAssistant) -> None:
    """Two supported devices must show the picker form so the user can choose."""
    d1 = _make_device("dreame.vacuum.p2008", did="d1")
    d2 = _make_device("viomi.vacuum.v12", did="d2")

    result = await _credentials_to_devices(hass, [d1, d2])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "devices"


async def test_cloud_device_picker_selection_creates_entry(hass: HomeAssistant) -> None:
    """Selecting from the picker creates the chosen device's entry."""
    d1 = _make_device("dreame.vacuum.p2008", did="d1")
    d2 = _make_device("viomi.vacuum.v12", did="d2")

    result = await _credentials_to_devices(hass, [d1, d2])
    assert result["step_id"] == "devices"

    with patch(
        "custom_components.xiaomi_vac.config_flow.IjaiVacuumDevice.get_wifi_sn",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"device": "d2"}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"enable_miot_oauth": False}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MODEL] == "viomi.vacuum.v12"


async def test_local_step_duplicate_unique_id_aborts(hass: HomeAssistant) -> None:
    """Configuring a device whose MAC is already set up must abort."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="AA:BB:CC:DD:EE:99",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN, CONF_MODEL: "dreame.vacuum.p2008"},
    )
    existing.add_to_hass(hass)

    form = await _open_local_form(hass)

    with patch(
        "custom_components.xiaomi_vac.config_flow._probe",
        return_value={"model": "dreame.vacuum.p2008", "mac": "AA:BB:CC:DD:EE:99"},
    ):
        result = await hass.config_entries.flow.async_configure(
            form["flow_id"], {CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ---------------------------------------------------------------------------
# Reconfigure: refresh host, token and server from the cloud
# ---------------------------------------------------------------------------

_CLOUD_DEVICE_LIST = "custom_components.xiaomi_vac.config_flow.XiaomiCloud._call"
_CLOUD_REFRESH = "custom_components.xiaomi_vac.config_flow.XiaomiCloud.refresh"

_STALE = {CONF_HOST: "10.0.0.121", CONF_TOKEN: "a" * 32, CONF_SERVER: "de"}
_CURRENT = {
    CONF_HOST: "10.0.0.92", CONF_TOKEN: "b" * 32, CONF_SERVER: "ru", CONF_OWNER_UID: OWNER_UID,
}


def _cloud_entry(hass: HomeAssistant, server: str = "de") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="AA:BB:CC:DD:EE:01",
        options={"keep": "me"},
        data={
            CONF_USERNAME: "user@example.com",
            CONF_MODEL: "ijai.vacuum.v3",
            CONF_DEVICE_ID: "1001",
            CONF_HOST: _STALE[CONF_HOST],
            CONF_TOKEN: _STALE[CONF_TOKEN],
            CONF_SERVER: server,
            CONF_USER_ID: "uid",
            CONF_SSECURITY: "sec",
            CONF_SERVICE_TOKEN: "svc",
            CONF_PASS_TOKEN: "pass",
            CONF_WIFI_SN: "SN1",
            CONF_OAUTH_ACCESS_TOKEN: "oauth-access",
            CONF_OAUTH_REGION: "ru",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _region_record(region: str, did: str = "1001", online: bool = True) -> dict:
    values = _STALE if region == "de" else _CURRENT
    return {
        "name": "vac", "did": did, "model": "ijai.vacuum.v3",
        "mac": "AA:BB:CC:DD:EE:01", "localip": values[CONF_HOST],
        "token": values[CONF_TOKEN], "isOnline": online, "uid": OWNER_UID,
    }


def _by_region(regions: dict[str, list[dict]], session: dict | None = None):
    """Fake `_call`: each region answers its own device list; `session` gates answers."""

    def fake(self, url: str, params: dict):
        if session is not None and not session["valid"]:
            return None
        for region, devices in regions.items():
            if url == self._api_url(region) + "/home/device_list":
                return _answer(devices)
        return None

    return fake


async def _run_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Open Reconfigure, confirm the form, return the final flow result."""
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    return result


async def test_reconfigure_found_updates_host_token_and_server(
    hass: HomeAssistant,
) -> None:
    """A did found with a current record gets that record's host, token and server."""
    entry = _cloud_entry(hass)
    before = dict(entry.data)

    with patch(
        _CLOUD_DEVICE_LIST,
        autospec=True,
        side_effect=_by_region(
            {"de": [_region_record("de", online=False)], "ru": [_region_record("ru")]}
        ),
    ):
        result = await _run_reconfigure(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data == {**before, **_CURRENT}
    assert entry.unique_id == "AA:BB:CC:DD:EE:01"
    assert entry.options == {"keep": "me"}


async def test_reconfigure_tie_keeps_stored_server(hass: HomeAssistant) -> None:
    """When no region is online, an entry stays on its stored server."""
    entry = _cloud_entry(hass, server="ru")

    with patch(
        _CLOUD_DEVICE_LIST,
        autospec=True,
        side_effect=_by_region(
            {
                "de": [_region_record("de", online=False)],
                "ru": [_region_record("ru", online=False)],
            }
        ),
    ):
        result = await _run_reconfigure(hass, entry)

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SERVER] == "ru"
    assert entry.data[CONF_HOST] == _CURRENT[CONF_HOST]
    assert entry.data[CONF_TOKEN] == _CURRENT[CONF_TOKEN]


async def test_reconfigure_did_not_found_aborts_and_leaves_entry(
    hass: HomeAssistant,
) -> None:
    """Discovery that does not return the entry's did aborts without changes."""
    entry = _cloud_entry(hass)
    before = dict(entry.data)

    with patch(
        _CLOUD_DEVICE_LIST,
        autospec=True,
        side_effect=_by_region({"ru": [_region_record("ru", did="9999")]}),
    ):
        result = await _run_reconfigure(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_device_not_found"
    assert entry.data == before


async def test_reconfigure_expired_session_refreshes_then_updates(
    hass: HomeAssistant,
) -> None:
    """A dead session is renewed from the pass token, then the entry is refreshed."""
    entry = _cloud_entry(hass)
    session = {"valid": False}

    def _renew(self) -> bool:
        session["valid"] = True
        return True

    with (
        patch(
            _CLOUD_DEVICE_LIST,
            autospec=True,
            side_effect=_by_region({"ru": [_region_record("ru")]}, session=session),
        ),
        patch(_CLOUD_REFRESH, autospec=True, side_effect=_renew),
    ):
        result = await _run_reconfigure(hass, entry)

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SERVER] == "ru"
    assert entry.data[CONF_TOKEN] == _CURRENT[CONF_TOKEN]
    assert entry.data[CONF_SERVICE_TOKEN] == "svc"


async def test_reconfigure_expired_session_refresh_fails_asks_for_reauth(
    hass: HomeAssistant,
) -> None:
    """A dead session that cannot be renewed aborts and leaves the entry alone."""
    entry = _cloud_entry(hass)
    before = dict(entry.data)

    with (
        patch(_CLOUD_DEVICE_LIST, autospec=True, side_effect=_by_region({})),
        patch(_CLOUD_REFRESH, return_value=False),
    ):
        result = await _run_reconfigure(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_reauth_required"
    assert entry.data == before


async def test_reconfigure_cloud_unreachable_aborts_and_leaves_entry(
    hass: HomeAssistant,
) -> None:
    """A renewal that cannot reach Xiaomi aborts as no_server_response."""
    entry = _cloud_entry(hass)
    before = dict(entry.data)

    with (
        patch(_CLOUD_DEVICE_LIST, autospec=True, side_effect=_by_region({})),
        patch(_CLOUD_REFRESH, side_effect=CloudUnreachable("down")),
    ):
        result = await _run_reconfigure(hass, entry)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_server_response"
    assert entry.data == before


async def test_reconfigure_local_only_entry_aborts_and_leaves_entry(
    hass: HomeAssistant,
) -> None:
    """A local-only entry has no cloud session, so Reconfigure aborts untouched."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        unique_id="AA:BB:CC:DD:EE:02",
        data={CONF_HOST: "1.2.3.4", CONF_TOKEN: TOKEN, CONF_MODEL: "dreame.vacuum.p2008"},
    )
    entry.add_to_hass(hass)
    before = dict(entry.data)

    result = await entry.start_reconfigure_flow(hass)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_local_only"
    assert entry.data == before
