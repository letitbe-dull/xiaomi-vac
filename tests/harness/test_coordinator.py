"""Harness tests for XiaomiVacuumCoordinator and XiaomiMapCoordinator."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
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
from custom_components.xiaomi_vac.map import MapResult, SessionExpired
from custom_components.xiaomi_vac.cloud.mqtt import MqttMessage
from custom_components.xiaomi_vac.map_coordinator import XiaomiMapCoordinator


def _fake_result(map_id: int = 1, content_hash: str = "hash-a") -> MapResult:
    """A real MapResult (not a MagicMock) — Phase 2's cache upsert calls
    base64.b64encode on `.image_png`, which a MagicMock attribute can't survive."""
    return MapResult(
        image_png=f"png-{map_id}".encode(),
        attributes={"map_id": map_id},
        vector={"map_id": map_id},
        map_id=map_id,
        content_hash=content_hash,
    )


class _FakeCache:
    """Stand-in for MapCache: same shape, no real HA Store I/O.

    Phase 2's coordinator always serves from what's in the cache (a live
    decode is upserted before serving), so these tests need something that
    round-trips upserts without touching disk.
    """

    def __init__(self) -> None:
        self._maps: dict[int, SimpleNamespace] = {}

    def get(self, map_id):
        return self._maps.get(map_id)

    def all(self):
        return dict(self._maps)

    async def async_upsert(self, map_id, *, png, attributes, vector, content_hash, timestamp):
        self._maps[map_id] = SimpleNamespace(
            png=png, attributes=attributes, vector=vector,
            content_hash=content_hash, timestamp=timestamp,
        )
        return True

    async def async_prune(self, keep_ids):
        before = set(self._maps)
        self._maps = {k: v for k, v in self._maps.items() if k in keep_ids}
        return before != set(self._maps)


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
    device.map_list.return_value = []
    control = MagicMock()
    control.data = None

    coord = XiaomiMapCoordinator(hass, entry, device, control)

    fake_fetcher = MagicMock()
    # slot "0" decodes, slot "1" doesn't — either is enough to serve a map.
    fake_fetcher.fetch.side_effect = [_fake_result(map_id=1), None]

    with (
        patch("custom_components.xiaomi_vac.map_coordinator.XiaomiCloud"),
        patch.object(coord, "_build", return_value=fake_fetcher),
        patch.object(coord, "_ensure_cache", new=AsyncMock(return_value=_FakeCache())),
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
    device.profile.brand = "ijai"          # drives parser_key() -> required key inputs
    device.profile.map = None              # no upload action to derive
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


async def test_map_coordinator_persists_live_wifi_sn_and_mac(
    hass: HomeAssistant,
) -> None:
    """Live non-empty map key inputs are saved back to the config entry."""
    entry = _map_entry("ijai.vacuum.v17")
    device = MagicMock()
    device.profile = SimpleNamespace(brand="ijai", profile_id="ijai.v17", map=None)
    device.get_wifi_sn.return_value = "A" * 18
    device.get_mac.return_value = "AA:BB:CC:DD:EE:FF"
    control = MagicMock()
    control.data = None
    coord = XiaomiMapCoordinator(hass, entry, device, control)

    with (
        patch("custom_components.xiaomi_vac.map_coordinator.XiaomiCloud"),
        patch("custom_components.xiaomi_vac.map_coordinator.MapFetcher"),
        patch.object(hass.config_entries, "async_update_entry") as update,
    ):
        coord._build()
        coord._persist_pending_entry_updates()

    updated = update.call_args.kwargs["data"]
    assert updated[CONF_WIFI_SN] == "A" * 18
    assert updated[CONF_MAC] == "AA:BB:CC:DD:EE:FF"


async def test_map_coordinator_never_overwrites_stored_keys_with_blank_live_reads(
    hass: HomeAssistant,
) -> None:
    """Blank live reads fall back to stored wifi_sn/mac without persisting blanks."""
    entry = _map_entry("ijai.vacuum.v17")
    entry.data[CONF_WIFI_SN] = "B" * 18
    entry.data[CONF_MAC] = "11:22:33:44:55:66"
    device = MagicMock()
    device.profile = SimpleNamespace(brand="ijai", profile_id="ijai.v17", map=None)
    device.get_wifi_sn.return_value = ""
    device.get_mac.return_value = None
    control = MagicMock()
    control.data = None
    coord = XiaomiMapCoordinator(hass, entry, device, control)

    with (
        patch("custom_components.xiaomi_vac.map_coordinator.XiaomiCloud"),
        patch("custom_components.xiaomi_vac.map_coordinator.MapFetcher") as fetcher,
        patch.object(hass.config_entries, "async_update_entry") as update,
    ):
        coord._build()
        coord._persist_pending_entry_updates()

    assert fetcher.call_args.kwargs["wifi_sn"] == "B" * 18
    assert fetcher.call_args.kwargs["mac"] == "11:22:33:44:55:66"
    update.assert_not_called()


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
    """SessionExpired from a slot fetch → _refresh_and_persist called, retry succeeds."""
    coord = _map_coord(hass)

    fake_result = _fake_result(map_id=1)
    fetcher = MagicMock()
    # First attempt: slot "0" raises SessionExpired (short-circuits slot "1").
    # Retry after refresh: slot "0" decodes, slot "1" doesn't.
    fetcher.fetch.side_effect = [SessionExpired("token dead"), fake_result, None]
    coord._fetcher = fetcher

    async def _exec(fn, *a):
        return fn(*a)

    mock_refresh = AsyncMock(return_value=True)
    with (
        patch.object(coord, "_refresh_and_persist", new=mock_refresh),
        patch.object(coord, "_ensure_cache", new=AsyncMock(return_value=_FakeCache())),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        result = await coord._async_update_data()

    assert result.map_id == fake_result.map_id
    assert result.image_png == fake_result.image_png
    mock_refresh.assert_awaited_once()


async def test_map_coordinator_session_expiry_refresh_fails_raises_auth_failed(
    hass: HomeAssistant,
) -> None:
    """If passToken refresh itself fails, ConfigEntryAuthFailed must be raised."""
    coord = _map_coord(hass)

    fetcher = MagicMock()
    fetcher.fetch.side_effect = SessionExpired("dead")
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
    """Both slots decoding to None (Key B / bad AES key) with nothing cached
    yet resets the fetcher and raises UpdateFailed."""
    coord = _map_coord(hass)

    fetcher = MagicMock()
    fetcher.fetch.return_value = None   # both slots: decode failure
    coord._fetcher = fetcher

    async def _exec(fn, *a):
        return fn(*a)

    with (
        patch.object(coord, "_ensure_cache", new=AsyncMock(return_value=_FakeCache())),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
        pytest.raises(UpdateFailed, match="Waiting for a readable map"),
    ):
        await coord._async_update_data()

    # Fetcher must be cleared so next cycle re-reads live key inputs.
    assert coord._fetcher is None


async def test_map_coordinator_stale_key_rebuild_on_next_cycle(
    hass: HomeAssistant,
) -> None:
    """After a None result clears the fetcher, the next call rebuilds it via _build."""
    coord = _map_coord(hass)

    fake_result = _fake_result(map_id=1)
    good_fetcher = MagicMock()
    good_fetcher.fetch.return_value = fake_result

    null_fetcher = MagicMock()
    null_fetcher.fetch.return_value = None

    coord._fetcher = null_fetcher

    async def _exec(fn, *a):
        return fn(*a)

    # First call: both slots None → fetcher reset, UpdateFailed raised.
    with (
        patch.object(coord, "_ensure_cache", new=AsyncMock(return_value=_FakeCache())),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
        pytest.raises(UpdateFailed),
    ):
        await coord._async_update_data()

    assert coord._fetcher is None

    # Second call: _build called (returns good fetcher), map succeeds.
    with (
        patch.object(coord, "_build", return_value=good_fetcher),
        patch.object(coord, "_ensure_cache", new=AsyncMock(return_value=_FakeCache())),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        result = await coord._async_update_data()

    assert result.map_id == fake_result.map_id
    assert result.image_png == fake_result.image_png


async def test_map_coordinator_no_fetcher_calls_build(
    hass: HomeAssistant,
) -> None:
    """On first update (no fetcher yet) _build is called to create one."""
    coord = _map_coord(hass)
    assert coord._fetcher is None

    fake_result = _fake_result(map_id=1)
    fetcher = MagicMock()
    fetcher.fetch.return_value = fake_result

    async def _exec(fn, *a):
        return fn(*a)

    with (
        patch.object(coord, "_build", return_value=fetcher),
        patch.object(coord, "_ensure_cache", new=AsyncMock(return_value=_FakeCache())),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        result = await coord._async_update_data()

    assert result.map_id == fake_result.map_id
    assert result.image_png == fake_result.image_png
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


# ---------------------------------------------------------------------------
# Map coordinator: two-slot fetch and cache-serve (Phase 5)
# ---------------------------------------------------------------------------


async def test_map_coordinator_two_slot_fallback_slot1_decodes(
    hass: HomeAssistant,
) -> None:
    """Slot 0 is Key B (None); slot 1 decrypts → result is served and cached.

    Both slots must always be tried (not short-circuited on the first None),
    and a Key-A decrypt from either slot must update the cache."""
    coord = _map_coord(hass)

    slot1 = _fake_result(map_id=1, content_hash="hash-slot1")
    fetcher = MagicMock()
    fetcher.fetch.side_effect = [None, slot1]  # slot "0" Key B, slot "1" Key A
    coord._fetcher = fetcher

    fake_cache = _FakeCache()

    async def _exec(fn, *a):
        return fn(*a)

    with (
        patch.object(coord, "_ensure_cache", new=AsyncMock(return_value=fake_cache)),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        result = await coord._async_update_data()

    # Slot 1 result must be served.
    assert result is not None
    assert result.image_png == slot1.image_png
    assert result.content_hash == slot1.content_hash

    # Both slots were tried — fetch called exactly twice.
    assert fetcher.fetch.call_count == 2

    # Cache must hold slot 1's render (verifies the upsert path ran).
    cached = fake_cache.get(1)
    assert cached is not None
    assert cached.png == slot1.image_png
    assert cached.content_hash == slot1.content_hash


async def test_map_coordinator_serves_from_cache_when_both_slots_key_b(
    hass: HomeAssistant,
) -> None:
    """Both slots Key B (None) but a prior Key-A render is cached → camera stays live.

    Verifies the core map-reliability success metric: a Key-B upload never
    blanks the camera when the cache holds a readable copy. The served
    MapResult must be byte-for-byte identical to what a live decode would
    produce (serve-parity)."""
    coord = _map_coord(hass)
    coord._device.map_list.return_value = [{"id": 42, "cur": True, "name": "Ground Floor"}]

    fetcher = MagicMock()
    fetcher.fetch.return_value = None  # both slots Key B this cycle
    coord._fetcher = fetcher

    fake_cache = _FakeCache()
    await fake_cache.async_upsert(
        42,
        png=b"ground-floor-prior-render",
        attributes={"rooms": 3},
        vector={"map_id": 42},
        content_hash="hash-prior",
        timestamp=500.0,
    )

    async def _exec(fn, *a):
        return fn(*a)

    with (
        patch.object(coord, "_ensure_cache", new=AsyncMock(return_value=fake_cache)),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        result = await coord._async_update_data()

    # Cached render served without raising UpdateFailed — camera stays available.
    assert result is not None
    assert result.image_png == b"ground-floor-prior-render"
    assert result.attributes == {"rooms": 3}
    assert result.content_hash == "hash-prior"
# ---------------------------------------------------------------------------
# Map refresh movement action
# ---------------------------------------------------------------------------


def _refreshable_map_coord(
    hass: HomeAssistant, activity: str = "docked",
) -> XiaomiMapCoordinator:
    coord = _map_coord(hass)
    coord._device.profile.map = object()
    coord._device.core.start = object()
    coord._device.core.charge = object()
    coord._device.start = MagicMock()
    coord._device.return_home = MagicMock()
    coord._control.data = SimpleNamespace(activity=activity)
    coord._control.async_request_refresh = AsyncMock()
    return coord


async def test_refresh_map_with_movement_requires_confirmation(
    hass: HomeAssistant,
) -> None:
    coord = _refreshable_map_coord(hass)

    with pytest.raises(HomeAssistantError, match="confirm_movement"):
        await coord.async_refresh_map_with_movement(
            confirm_movement=False, use_mqtt=True,
        )

    coord._device.start.assert_not_called()
    coord._device.return_home.assert_not_called()


async def test_refresh_map_with_movement_refuses_when_not_docked_or_idle(
    hass: HomeAssistant,
) -> None:
    coord = _refreshable_map_coord(hass, activity="cleaning")

    with pytest.raises(HomeAssistantError, match="docked or idle"):
        await coord.async_refresh_map_with_movement(
            confirm_movement=True, use_mqtt=True,
        )

    coord._device.start.assert_not_called()
    coord._device.return_home.assert_not_called()


async def test_refresh_map_with_movement_refuses_single_flight(
    hass: HomeAssistant,
) -> None:
    coord = _refreshable_map_coord(hass)
    await coord._refresh_map_lock.acquire()
    try:
        with pytest.raises(HomeAssistantError, match="already running"):
            await coord.async_refresh_map_with_movement(
                confirm_movement=True, use_mqtt=True,
            )
    finally:
        coord._refresh_map_lock.release()

    coord._device.start.assert_not_called()


async def test_refresh_map_with_movement_uses_mqtt_wait_and_docks(
    hass: HomeAssistant,
) -> None:
    coord = _refreshable_map_coord(hass)

    async def _exec(fn, *args):
        return fn(*args)

    wait = AsyncMock()
    with (
        patch.object(coord, "_async_wait_for_mqtt_upload", new=wait),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        await coord.async_refresh_map_with_movement(
            confirm_movement=True, use_mqtt=True,
        )

    coord._device.start.assert_called_once()
    coord._device.return_home.assert_called_once()
    wait.assert_awaited_once()


async def test_refresh_map_with_movement_fallback_polls_and_docks(
    hass: HomeAssistant,
) -> None:
    coord = _refreshable_map_coord(hass)

    async def _exec(fn, *args):
        return fn(*args)

    poll = AsyncMock()
    with (
        patch.object(coord, "_async_poll_for_live_map", new=poll),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        await coord.async_refresh_map_with_movement(
            confirm_movement=True, use_mqtt=False,
        )

    coord._device.start.assert_called_once()
    coord._device.return_home.assert_called_once()
    poll.assert_awaited_once_with(0.0)


async def test_refresh_map_with_movement_docks_even_when_start_raises(
    hass: HomeAssistant,
) -> None:
    """If start() throws, the vacuum must still return to dock (finally guarantee)."""
    coord = _refreshable_map_coord(hass)
    coord._device.start.side_effect = Exception("motor failure")

    async def _exec(fn, *args):
        return fn(*args)

    with (
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
        pytest.raises(Exception, match="motor failure"),
    ):
        await coord.async_refresh_map_with_movement(confirm_movement=True, use_mqtt=True)

    coord._device.return_home.assert_called_once()


# ---------------------------------------------------------------------------
# MQTT event → debounced fetch
# ---------------------------------------------------------------------------


async def test_mqtt_upload_event_schedules_debounce_task(hass: HomeAssistant) -> None:
    """event_occured/10/6 creates a debounce task on the coordinator."""
    coord = _map_coord(hass)
    coord.async_refresh = AsyncMock()

    msg = MqttMessage(kind="event", topic="x", siid=10, eiid=6)
    assert coord._mqtt_debounce_task is None

    with patch("custom_components.xiaomi_vac.map_coordinator._DEBOUNCE_SECONDS", 60):
        await coord.async_on_mqtt_message(msg)

    assert coord._mqtt_debounce_task is not None
    coord._mqtt_debounce_task.cancel()  # tidy up


async def test_map_upload_request_uses_active_map_and_throttles(
    hass: HomeAssistant,
) -> None:
    """Coordinator upload requests use the active map id and throttle repeats."""
    coord = _map_coord(hass)
    coord._map_list_meta = [{"id": 7, "cur": True, "name": "Upstairs"}]

    async def _exec(fn, *args):
        return fn(*args)

    with (
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
        patch("custom_components.xiaomi_vac.map_coordinator.time.monotonic",
              side_effect=[100.0, 101.0]),
    ):
        first = await coord.async_request_map_upload()
        second = await coord.async_request_map_upload()

    assert first is True
    assert second is False
    coord._device.request_map_upload.assert_called_once_with(7)


async def test_mqtt_curmap_event_uploads_selected_map_and_refreshes(
    hass: HomeAssistant,
) -> None:
    """MQTT curMapId changes trigger one upload wrapper call and one refresh."""
    coord = _map_coord(hass)
    coord.async_refresh = AsyncMock()

    async def _exec(fn, *args):
        return fn(*args)

    msg = MqttMessage(kind="property", topic="x", siid=10, piid=2, value=7)
    with (
        patch("custom_components.xiaomi_vac.map_coordinator._DEBOUNCE_SECONDS", 0),
        patch.object(hass, "async_add_executor_job", new=AsyncMock(side_effect=_exec)),
    ):
        await coord.async_on_mqtt_message(msg)
        await asyncio.sleep(0.05)

    coord._device.request_map_upload.assert_called_once_with(7)
    coord.async_refresh.assert_awaited_once()


async def test_mqtt_curmap_property_updates_active_id(hass: HomeAssistant) -> None:
    """properties_changed/10/2 stores the new curMapId on the coordinator."""
    coord = _map_coord(hass)
    coord.async_refresh = AsyncMock()

    msg = MqttMessage(kind="property", topic="x", siid=10, piid=2, value=7)

    with patch("custom_components.xiaomi_vac.map_coordinator._DEBOUNCE_SECONDS", 60):
        await coord.async_on_mqtt_message(msg)

    assert coord._mqtt_active_id == 7
    coord._mqtt_debounce_task.cancel()


async def test_mqtt_burst_coalesces_to_single_refresh(hass: HomeAssistant) -> None:
    """Three rapid upload events within the debounce window trigger one async_refresh."""
    coord = _map_coord(hass)
    coord.async_refresh = AsyncMock()

    msg = MqttMessage(kind="event", topic="x", siid=10, eiid=6)

    with patch("custom_components.xiaomi_vac.map_coordinator._DEBOUNCE_SECONDS", 0):
        await coord.async_on_mqtt_message(msg)
        await coord.async_on_mqtt_message(msg)
        await coord.async_on_mqtt_message(msg)
        await asyncio.sleep(0.05)

    coord.async_refresh.assert_awaited_once()
