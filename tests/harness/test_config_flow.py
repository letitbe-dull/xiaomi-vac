"""Tests for the Xiaomi Vacuum config flow."""
import asyncio
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.xiaomi_vac import async_migrate_entry
from custom_components.xiaomi_vac.const import (
    CONF_HOST,
    CONF_MODEL,
    CONF_OAUTH_ACCESS_TOKEN,
    CONF_OAUTH_DEVICE_ID,
    CONF_OAUTH_EXPIRES_TS,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_OAUTH_REGION,
    CONF_OAUTH_REDIRECT_URI,
    CONF_PASS_TOKEN,
    CONF_PASSWORD,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_TOKEN,
    CONF_USER_ID,
    CONF_USERNAME,
    DOMAIN,
)

TOKEN = "0" * 32
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
        "token": TOKEN, "server": "cn",
    }


def _cloud_patches(login_state: str = "ok", devices: list | None = None):
    """Context-manager stack: stub login, device list, and wifi_sn fetch."""
    if devices is None:
        devices = []
    return [
        patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.begin_login",
            return_value=login_state,
        ),
        patch(
            "custom_components.xiaomi_vac.config_flow.XiaomiCloud.list_vacuums",
            return_value=devices,
        ),
        patch(
            "custom_components.xiaomi_vac.config_flow.IjaiVacuumDevice.get_wifi_sn",
            return_value=None,
        ),
    ]


async def _credentials_to_devices(
    hass: HomeAssistant, devices: list, *, stop_at_oauth: bool = False
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
        for p in _cloud_patches(devices=devices):
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


async def test_cloud_no_vacuums_aborts(hass: HomeAssistant) -> None:
    """An account with no vacuum devices aborts with no_devices."""
    result = await _credentials_to_devices(hass, [])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices"


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
