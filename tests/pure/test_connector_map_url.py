"""Pure tests for XiaomiCloud.map_url()'s endpoint-fallback logic.

Live-diagnosed 2026-07-31: for some accounts/models (observed: 3irobotic-
manufactured xiaomi.* models like ov42gl) the "wrong" endpoint fails with
code -6 ("invalid config for fds"), not the -8 the old fallback trigger
checked for. map_url() must now try the alternate endpoint on ANY failure,
not just a specific error code.
"""
from __future__ import annotations

from unittest.mock import patch

from cloud.connector import XiaomiCloud


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
