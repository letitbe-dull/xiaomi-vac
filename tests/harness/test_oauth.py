"""Tests for MIoT OAuth token helpers."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs, urlparse

from custom_components.xiaomi_vac.cloud.oauth import (
    OAUTH_APP_ID,
    build_authorize_url,
    exchange_code,
    refreshed_oauth_entry_updates,
    resolve_region_from_code,
)
from custom_components.xiaomi_vac.const import (
    CONF_OAUTH_ACCESS_TOKEN,
    CONF_OAUTH_DEVICE_ID,
    CONF_OAUTH_EXPIRES_TS,
    CONF_OAUTH_REFRESH_TOKEN,
    CONF_OAUTH_REGION,
    CONF_OAUTH_REDIRECT_URI,
)


class _Response:
    status_code = 200

    def json(self) -> dict:
        return {
            "code": 0,
            "result": {
                "access_token": "access",
                "refresh_token": "refresh2",
                "expires_in": 1000,
            },
        }


class _Session:
    def __init__(self) -> None:
        self.url = ""
        self.params: dict = {}

    def get(self, url: str, **kwargs) -> _Response:
        self.url = url
        self.params = kwargs["params"]
        return _Response()


def test_build_authorize_url_uses_xiaomi_state() -> None:
    """The authorize URL matches Xiaomi's MIoT OAuth contract."""
    device_id = "ha.0123456789abcdef"
    redirect_uri = "http://homeassistant.local:8123/api/webhook/test"
    query = parse_qs(
        urlparse(build_authorize_url(device_id, redirect_uri=redirect_uri)).query
    )

    assert query["client_id"] == [OAUTH_APP_ID]
    assert query["redirect_uri"] == [redirect_uri]
    assert query["response_type"] == ["code"]
    assert query["device_id"] == [device_id]
    assert query["skip_confirm"] == ["true"]
    assert query["state"] == [hashlib.sha1(f"d={device_id}".encode()).hexdigest()]


def test_resolve_region_from_code_prefix() -> None:
    """Auth-code prefixes carry the OAuth/MQTT account region."""
    assert resolve_region_from_code("ALSG_example", "tw") == "sg"
    assert resolve_region_from_code("unknown", "de") == "de"
    assert resolve_region_from_code("unknown", "tw") is None


def test_exchange_code_parses_tokens_and_early_expiry() -> None:
    """Token exchange stores expiry at 70 percent of Xiaomi's lifetime."""
    session = _Session()

    tokens = exchange_code(
        "ALSG_code",
        "ha.0123456789abcdef",
        "sg",
        redirect_uri="http://ha.local/callback",
        session=session,
        now=100,
    )

    assert session.url == "https://sg.ha.api.io.mi.com/app/v2/ha/oauth/get_token"
    payload = json.loads(session.params["data"])
    assert payload["code"] == "ALSG_code"
    assert payload["redirect_uri"] == "http://ha.local/callback"
    assert payload["device_id"] == "ha.0123456789abcdef"
    assert tokens.access_token == "access"
    assert tokens.refresh_token == "refresh2"
    assert tokens.expires_ts == 800
    assert tokens.region == "sg"


def test_oauth_needs_refresh_when_expired() -> None:
    """oauth_needs_refresh returns True when the stored expiry is in the past."""
    from custom_components.xiaomi_vac.cloud.oauth import oauth_needs_refresh

    data = {
        CONF_OAUTH_REFRESH_TOKEN: "tok",
        CONF_OAUTH_REGION: "sg",
        CONF_OAUTH_EXPIRES_TS: 99,
    }
    assert oauth_needs_refresh(data, now=100) is True


def test_oauth_needs_refresh_when_not_due() -> None:
    """oauth_needs_refresh returns False when the token is still fresh."""
    from custom_components.xiaomi_vac.cloud.oauth import oauth_needs_refresh

    data = {
        CONF_OAUTH_REFRESH_TOKEN: "tok",
        CONF_OAUTH_REGION: "sg",
        CONF_OAUTH_EXPIRES_TS: 9999,
    }
    assert oauth_needs_refresh(data, now=100) is False


def test_oauth_needs_refresh_missing_token_returns_false() -> None:
    """oauth_needs_refresh returns False when no refresh token is stored."""
    from custom_components.xiaomi_vac.cloud.oauth import oauth_needs_refresh

    assert oauth_needs_refresh({CONF_OAUTH_REGION: "sg", CONF_OAUTH_EXPIRES_TS: 0}) is False


def test_refreshed_updates_returns_none_when_not_due() -> None:
    """refreshed_oauth_entry_updates is a no-op when the token is still fresh."""
    data = {
        CONF_OAUTH_REFRESH_TOKEN: "tok",
        CONF_OAUTH_REGION: "sg",
        CONF_OAUTH_DEVICE_ID: "ha.abc",
        CONF_OAUTH_EXPIRES_TS: 9999,
    }
    result = refreshed_oauth_entry_updates(data, force=False, now=100)
    assert result is None


def test_refreshed_oauth_entry_updates_force_refresh() -> None:
    """Forced refresh returns data updates for MQTT auth rejection handling."""
    session = _Session()
    updates = refreshed_oauth_entry_updates(
        {
            CONF_OAUTH_REFRESH_TOKEN: "refresh1",
            CONF_OAUTH_REGION: "sg",
            CONF_OAUTH_DEVICE_ID: "ha.0123456789abcdef",
            CONF_OAUTH_REDIRECT_URI: "http://ha.local/callback",
            CONF_OAUTH_EXPIRES_TS: 9999,
        },
        force=True,
        session=session,
        now=100,
    )

    payload = json.loads(session.params["data"])
    assert payload["refresh_token"] == "refresh1"
    assert payload["redirect_uri"] == "http://ha.local/callback"
    assert updates == {
        CONF_OAUTH_ACCESS_TOKEN: "access",
        CONF_OAUTH_REFRESH_TOKEN: "refresh2",
        CONF_OAUTH_EXPIRES_TS: 800,
        CONF_OAUTH_REGION: "sg",
        CONF_OAUTH_DEVICE_ID: "ha.0123456789abcdef",
        CONF_OAUTH_REDIRECT_URI: "http://ha.local/callback",
    }
