"""Tests for MIoT cloud MQTT client and message parser."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.xiaomi_vac.cloud.mqtt import (
    MiotMqttClient,
    MqttMessage,
    _parse_message,
)


# ---------------------------------------------------------------------------
# _parse_message
# ---------------------------------------------------------------------------


def test_parse_properties_changed() -> None:
    """properties_changed topic yields a property MqttMessage."""
    topic = "device/did123/up/properties_changed/10/2"
    payload = b'{"value": 42}'

    msg = _parse_message(topic, payload)

    assert msg is not None
    assert msg.kind == "property"
    assert msg.siid == 10
    assert msg.piid == 2
    assert msg.value == 42


def test_parse_event_occured() -> None:
    """event_occured topic yields an event MqttMessage with arguments."""
    topic = "device/did123/up/event_occured/10/6"
    payload = b'{"arguments": [{"piid": 20, "value": 1}]}'

    msg = _parse_message(topic, payload)

    assert msg is not None
    assert msg.kind == "event"
    assert msg.siid == 10
    assert msg.eiid == 6
    assert isinstance(msg.arguments, list)


def test_parse_other_topic_returns_other_kind() -> None:
    """Unrecognised subtopics (e.g. state/*) surface as kind='other'."""
    topic = "device/did123/state/online"
    msg = _parse_message(topic, b"")

    assert msg is not None
    assert msg.kind == "other"


def test_parse_too_short_topic_returns_none() -> None:
    """Fewer than 4 path segments → None (not our topic format)."""
    assert _parse_message("device/did", b"") is None


def test_parse_malformed_json_does_not_raise() -> None:
    """Bad JSON payload → value/arguments are None, no exception raised."""
    topic = "device/did/up/properties_changed/2/1"
    msg = _parse_message(topic, b"{not json}")

    assert msg is not None
    assert msg.kind == "property"
    assert msg.value is None


# ---------------------------------------------------------------------------
# MiotMqttClient helpers
# ---------------------------------------------------------------------------


def _make_client(hass: HomeAssistant) -> MiotMqttClient:
    return MiotMqttClient(
        hass,
        region="sg",
        did="did123",
        client_device_id="ha.abc",
        access_token="tok",
        token_provider=AsyncMock(),
        on_message=AsyncMock(),
    )


# ---------------------------------------------------------------------------
# Lifecycle: start / stop
# ---------------------------------------------------------------------------


async def test_client_start_calls_paho_connect_and_loop_start(
    hass: HomeAssistant,
) -> None:
    """async_start queues the connection and starts paho's network loop."""
    client = _make_client(hass)
    paho_mock = MagicMock()

    with patch.object(client, "_build_client", return_value=paho_mock):
        await client.async_start()

    paho_mock.connect_async.assert_called_once()
    paho_mock.loop_start.assert_called_once()
    assert client._started is True


async def test_client_start_is_idempotent(hass: HomeAssistant) -> None:
    """Calling async_start twice does not open a second connection."""
    client = _make_client(hass)
    paho_mock = MagicMock()

    with patch.object(client, "_build_client", return_value=paho_mock):
        await client.async_start()
        await client.async_start()

    assert paho_mock.connect_async.call_count == 1


async def test_client_stop_shuts_down_paho(hass: HomeAssistant) -> None:
    """async_stop disconnects and joins the paho loop."""
    client = _make_client(hass)
    paho_mock = MagicMock()

    with patch.object(client, "_build_client", return_value=paho_mock):
        await client.async_start()

    # _shutdown_sync is called in the executor; patch async_add_executor_job to run it inline.
    async def _exec(fn, *args):
        return fn(*args)

    with patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)):
        await client.async_stop()

    paho_mock.disconnect.assert_called_once()
    paho_mock.loop_stop.assert_called_once()
    assert client._started is False


# ---------------------------------------------------------------------------
# _on_connect callbacks
# ---------------------------------------------------------------------------


def test_on_connect_rc0_subscribes_three_legs() -> None:
    """rc=0 → subscribe device/{did}/up/#, down/#, state/#."""
    hass = MagicMock()
    client = MiotMqttClient(
        hass,
        region="sg",
        did="did123",
        client_device_id="ha.abc",
        access_token="tok",
        token_provider=AsyncMock(),
        on_message=AsyncMock(),
    )
    paho_mock = MagicMock()

    client._on_connect(paho_mock, None, None, 0, None)

    paho_mock.subscribe.assert_has_calls(
        [
            call("device/did123/up/#"),
            call("device/did123/down/#"),
            call("device/did123/state/#"),
        ],
        any_order=False,
    )


async def test_on_connect_rc5_schedules_token_refresh(hass: HomeAssistant) -> None:
    """rc=5 (not authorised) triggers _refresh_and_reconnect on the event loop."""
    client = _make_client(hass)
    paho_mock = MagicMock()
    client._client = paho_mock
    client._started = True

    refresh_called = asyncio.Event()

    async def _fake_refresh():
        refresh_called.set()

    with patch.object(client, "_refresh_and_reconnect", side_effect=_fake_refresh):
        client._on_connect(paho_mock, None, None, 5, None)
        await asyncio.wait_for(refresh_called.wait(), timeout=1.0)

    assert refresh_called.is_set()


# ---------------------------------------------------------------------------
# Message dispatch (thread → event loop)
# ---------------------------------------------------------------------------


def test_on_paho_message_dispatches_via_call_soon_threadsafe() -> None:
    """Received messages are handed to the HA event loop via call_soon_threadsafe."""
    hass = MagicMock()
    client = MiotMqttClient(
        hass,
        region="sg",
        did="did123",
        client_device_id="ha.abc",
        access_token="tok",
        token_provider=AsyncMock(),
        on_message=AsyncMock(),
    )
    raw = MagicMock()
    raw.topic = "device/did123/up/properties_changed/10/2"
    raw.payload = b'{"value": 5}'

    client._on_paho_message(MagicMock(), None, raw)

    hass.loop.call_soon_threadsafe.assert_called_once()
    _fn, parsed = hass.loop.call_soon_threadsafe.call_args.args
    assert parsed.kind == "property"
    assert parsed.siid == 10
    assert parsed.piid == 2
