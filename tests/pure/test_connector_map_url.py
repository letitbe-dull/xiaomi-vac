"""Pure tests for XiaomiCloud.map_url()'s endpoint-fallback logic.

Live-diagnosed 2026-07-31: for some accounts/models (observed: 3irobotic-
manufactured xiaomi.* models like ov42gl) the "wrong" endpoint fails with
code -6 ("invalid config for fds"), not the -8 the old fallback trigger
checked for. map_url() must now try the alternate endpoint on ANY failure,
not just a specific error code.
"""
from __future__ import annotations

import base64
from unittest.mock import patch

import requests

import pytest

from cloud.connector import CloudUnreachable, XiaomiCloud


def _cloud() -> XiaomiCloud:
    cloud = XiaomiCloud("user@example.com")
    cloud.user_id = "9876543210"
    return cloud


def test_map_url_succeeds_on_first_endpoint_without_trying_alternate():
    cloud = _cloud()
    with patch.object(cloud, "_call", return_value={"code": 0, "result": {"url": "https://a"}}) as mock_call:
        url = cloud.map_url("de", "123", "0", endpoint="get_interim_file_url_pro")
    assert url == "https://a"
    mock_call.assert_called_once()


def test_map_url_falls_back_on_code_minus6_not_just_minus8():
    cloud = _cloud()
    responses = [
        {"code": -6, "message": "invalid config for fds", "result": None},
        {"code": 0, "message": "ok", "result": {"url": "https://alt"}},
    ]
    with patch.object(cloud, "_call", side_effect=responses) as mock_call:
        url = cloud.map_url("de", "123", "0", endpoint="get_interim_file_url")
    assert url == "https://alt"
    assert mock_call.call_count == 2
    # Second call must hit the *other* endpoint.
    second_url = mock_call.call_args_list[1].args[0]
    assert "get_interim_file_url_pro" in second_url


def test_map_url_still_falls_back_on_code_minus8():
    cloud = _cloud()
    responses = [
        {"code": -8, "message": "rejected", "result": None},
        {"code": 0, "message": "ok", "result": {"url": "https://alt"}},
    ]
    with patch.object(cloud, "_call", side_effect=responses):
        url = cloud.map_url("de", "123", "0", endpoint="get_interim_file_url_pro")
    assert url == "https://alt"


def test_map_url_returns_none_when_both_endpoints_fail():
    cloud = _cloud()
    responses = [
        {"code": -6, "message": "invalid config for fds", "result": None},
        {"code": -6, "message": "invalid config for fds", "result": None},
    ]
    with patch.object(cloud, "_call", side_effect=responses):
        url = cloud.map_url("de", "123", "0", endpoint="get_interim_file_url")
    assert url is None


def test_map_url_returns_none_when_call_itself_returns_none():
    """A non-200 HTTP status makes `_call` return None (see connector.py)."""
    cloud = _cloud()
    with patch.object(cloud, "_call", return_value=None):
        url = cloud.map_url("de", "123", "0")
    assert url is None


def test_call_returns_none_on_request_exception():
    """A timeout/DNS/connection error on one regional server must be swallowed
    (-> None) instead of propagating and aborting multi-region discovery —
    find_device()/list_vacuums() already treat a falsy result as "skip this
    server" (issue #42)."""
    cloud = _cloud()
    cloud.ssecurity = base64.b64encode(b"0123456789abcdef").decode()
    cloud.service_token = "svc"
    with patch.object(cloud._s, "post", side_effect=requests.exceptions.Timeout("boom")):
        result = cloud._call("https://de.api.io.mi.com/app/home/device_list", {"data": "{}"})
    assert result is None
    assert cloud.last_call_unreachable is True


def _calls(cloud, outcomes):
    """Fake `_call` that sets last_call_unreachable like the real one."""
    it = iter(outcomes)

    def _fake(url, params):
        resp = next(it)
        cloud.last_call_unreachable = resp == "timeout"
        return None if resp == "timeout" else resp
    return _fake


def test_map_url_raises_unreachable_when_both_endpoints_time_out():
    cloud = _cloud()
    with patch.object(cloud, "_call", side_effect=_calls(cloud, ["timeout", "timeout"])):
        with pytest.raises(CloudUnreachable):
            cloud.map_url("tw", "123", "0")


def test_map_url_returns_none_when_only_one_endpoint_times_out():
    cloud = _cloud()
    refused = {"code": -6, "message": "invalid config for fds", "result": None}
    with patch.object(cloud, "_call", side_effect=_calls(cloud, ["timeout", refused])):
        assert cloud.map_url("tw", "123", "0") is None
    with patch.object(cloud, "_call", side_effect=_calls(cloud, [refused, "timeout"])):
        assert cloud.map_url("tw", "123", "0") is None


def test_refresh_raises_unreachable_on_network_error():
    cloud = _cloud()
    cloud.pass_token = "pass"
    with patch.object(cloud._s, "get", side_effect=requests.exceptions.ReadTimeout("slow")):
        with pytest.raises(CloudUnreachable):
            cloud.refresh()


def test_refresh_returns_false_when_xiaomi_rejects_the_pass_token():
    cloud = _cloud()
    cloud.pass_token = "dead"
    rejected = type("R", (), {"text": '&&&START&&&{"code":70016}'})()
    with patch.object(cloud._s, "get", return_value=rejected):
        assert cloud.refresh() is False
