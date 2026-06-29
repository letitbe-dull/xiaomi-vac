"""Harness tests: entity construction, feature flags, command dispatch."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from homeassistant.components.vacuum import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.xiaomi_vac.device import VacuumStatus
from custom_components.xiaomi_vac.number import VolumeNumber, async_setup_entry as number_setup
from custom_components.xiaomi_vac.select import (
    XiaomiVacuumSelect,
    async_setup_entry as select_setup,
)
from custom_components.xiaomi_vac.switch import (
    AlarmSwitch,
    RepeatSwitch,
    async_setup_entry as switch_setup,
)
from custom_components.xiaomi_vac.vacuum import XiaomiVacuum, async_setup_entry as vacuum_setup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS = VacuumStatus(
    activity="docked",
    raw_status=0,
    battery=80,
    fault=0,
    fan_speed_raw=1,
    water_level_raw=2,
    mode_raw=None,
    sweep_type_raw=None,
    repeat_raw=1,
    alarm_raw=0,
    volume_raw=5,
    main_brush_life=90,
    side_brush_life=90,
    filter_life=90,
    mop_life=None,
    clean_area=0,
    clean_time=0,
)


def _make_coordinator(core_overrides: dict | None = None) -> MagicMock:
    """Return a minimal coordinator mock with configurable core attributes."""
    core = MagicMock()
    core.charge = MagicMock()      # return_home supported
    core.locate = MagicMock()      # locate supported
    core.alarm = MagicMock()       # alarm supported
    core.repeat = MagicMock()      # repeat supported
    core.volume = MagicMock()      # volume supported
    core.fan_speeds = {"quiet": 1, "normal": 2}
    core.water_levels = {"off": 0, "low": 1}
    core.modes = None              # no mode select
    core.sweep_types = None        # no sweep_type select

    if core_overrides:
        for k, v in core_overrides.items():
            setattr(core, k, v)

    device = MagicMock()
    device.model = "dreame.vacuum.p2008"
    device.core = core

    coordinator = MagicMock()
    coordinator.device = device
    coordinator.data = _STATUS
    coordinator.hass = MagicMock()
    return coordinator


def _make_entry(unique_id: str = "AA:BB:CC:DD:EE:FF") -> MagicMock:
    entry = MagicMock()
    entry.unique_id = unique_id
    entry.entry_id = "test_entry_id"
    entry.title = "Test Vacuum"
    entry.runtime_data = MagicMock()
    return entry


# ---------------------------------------------------------------------------
# Vacuum feature flags
# ---------------------------------------------------------------------------


def test_vacuum_base_features_always_present() -> None:
    """START, PAUSE, STOP, STATE are always set regardless of core."""
    core_overrides = {"charge": None, "locate": None, "alarm": None}
    coord = _make_coordinator(core_overrides)
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    flags = vac.supported_features
    assert flags & VacuumEntityFeature.START
    assert flags & VacuumEntityFeature.PAUSE
    assert flags & VacuumEntityFeature.STOP
    assert flags & VacuumEntityFeature.STATE


def test_vacuum_return_home_added_when_core_has_charge() -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    assert vac.supported_features & VacuumEntityFeature.RETURN_HOME


def test_vacuum_return_home_absent_when_no_charge() -> None:
    coord = _make_coordinator({"charge": None})
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    assert not (vac.supported_features & VacuumEntityFeature.RETURN_HOME)


def test_vacuum_locate_added_when_core_has_locate() -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    assert vac.supported_features & VacuumEntityFeature.LOCATE


def test_vacuum_locate_added_via_alarm_when_no_locate() -> None:
    """LOCATE is also set when only alarm is present (alarm IS the locate)."""
    coord = _make_coordinator({"locate": None})  # alarm still present
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    assert vac.supported_features & VacuumEntityFeature.LOCATE


def test_vacuum_locate_absent_when_neither_locate_nor_alarm() -> None:
    coord = _make_coordinator({"locate": None, "alarm": None})
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    assert not (vac.supported_features & VacuumEntityFeature.LOCATE)


# ---------------------------------------------------------------------------
# Select entity conditional creation
# ---------------------------------------------------------------------------


async def test_select_setup_creates_only_backed_selects(hass: HomeAssistant) -> None:
    """Only selects whose core attr is truthy should be created."""
    coord = _make_coordinator()  # fan_speeds + water_levels; no modes/sweep_types
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    # async_add_entities receives a generator; extend consumes it.
    await select_setup(hass, entry, lambda entities: added.extend(entities))

    keys = {e._key for e in added}
    assert "fan_speed" in keys
    assert "water_level" in keys
    assert "mode" not in keys
    assert "sweep_type" not in keys


async def test_select_setup_creates_nothing_when_all_absent(hass: HomeAssistant) -> None:
    overrides = {"fan_speeds": None, "water_levels": None, "modes": None, "sweep_types": None}
    coord = _make_coordinator(overrides)
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await select_setup(hass, entry, lambda entities: added.extend(entities))
    assert added == []


# ---------------------------------------------------------------------------
# Switch entity conditional creation
# ---------------------------------------------------------------------------


async def test_switch_setup_creates_repeat_and_alarm(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await switch_setup(hass, entry, lambda entities: added.extend(entities))

    types = {type(e) for e in added}
    assert RepeatSwitch in types
    assert AlarmSwitch in types


async def test_switch_setup_no_entities_when_absent(hass: HomeAssistant) -> None:
    coord = _make_coordinator({"repeat": None, "alarm": None})
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await switch_setup(hass, entry, lambda entities: added.extend(entities))
    assert added == []


# ---------------------------------------------------------------------------
# Number entity conditional creation
# ---------------------------------------------------------------------------


async def test_number_setup_creates_volume_when_supported(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await number_setup(hass, entry, lambda entities: added.extend(entities))
    assert len(added) == 1
    assert isinstance(added[0], VolumeNumber)


async def test_number_setup_no_entity_when_no_volume(hass: HomeAssistant) -> None:
    coord = _make_coordinator({"volume": None})
    entry = _make_entry()
    entry.runtime_data.control = coord

    added: list = []
    await number_setup(hass, entry, lambda entities: added.extend(entities))
    assert added == []


# ---------------------------------------------------------------------------
# Command dispatch: vacuum entity
# ---------------------------------------------------------------------------


async def test_vacuum_start_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    vac.hass = hass

    await vac.async_start()

    coord.device.start.assert_called_once()
    coord.async_request_refresh.assert_awaited_once()


async def test_vacuum_stop_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    vac.hass = hass

    await vac.async_stop()

    coord.device.stop.assert_called_once()
    coord.async_request_refresh.assert_awaited_once()


async def test_vacuum_pause_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    vac.hass = hass

    await vac.async_pause()

    coord.device.pause.assert_called_once()
    coord.async_request_refresh.assert_awaited_once()


async def test_vacuum_return_home_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    vac.hass = hass

    await vac.async_return_to_base()

    coord.device.return_home.assert_called_once()
    coord.async_request_refresh.assert_awaited_once()


async def test_vacuum_locate_calls_device(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    vac.hass = hass

    await vac.async_locate()

    coord.device.locate.assert_called_once()


async def test_vacuum_clean_segment_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()
    vac = XiaomiVacuum(coord, entry)
    vac.hass = hass

    await vac.async_clean_segment(segments=[1, 2])

    coord.device.clean_segments.assert_called_once_with([1, 2])
    coord.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Command dispatch: select entity
# ---------------------------------------------------------------------------


async def test_select_option_calls_setter_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()

    sel = XiaomiVacuumSelect(coord, entry, "fan_speed", "fan_speeds", "fan_speed_raw", "set_fan_speed")
    sel.hass = hass

    await sel.async_select_option("normal")

    coord.device.set_fan_speed.assert_called_once_with("normal")
    coord.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Command dispatch: switch entity (repeat + alarm)
# ---------------------------------------------------------------------------


async def test_repeat_switch_turn_on_calls_device(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()

    sw = RepeatSwitch(coord, entry)
    sw.hass = hass

    await sw.async_turn_on()

    coord.device.set_repeat.assert_called_once_with(True)
    coord.async_request_refresh.assert_awaited_once()


async def test_repeat_switch_turn_off_calls_device(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()

    sw = RepeatSwitch(coord, entry)
    sw.hass = hass

    await sw.async_turn_off()

    coord.device.set_repeat.assert_called_once_with(False)
    coord.async_request_refresh.assert_awaited_once()


async def test_alarm_switch_turn_on_calls_device(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()

    sw = AlarmSwitch(coord, entry)
    sw.hass = hass

    await sw.async_turn_on()

    coord.device.set_alarm.assert_called_once_with(True)
    coord.async_request_refresh.assert_awaited_once()


# ---------------------------------------------------------------------------
# Command dispatch: volume number
# ---------------------------------------------------------------------------


async def test_volume_set_value_calls_device_and_refreshes(hass: HomeAssistant) -> None:
    coord = _make_coordinator()
    coord.async_request_refresh = AsyncMock()
    entry = _make_entry()

    num = VolumeNumber(coord, entry)
    num.hass = hass

    await num.async_set_native_value(7.0)

    coord.device.set_volume.assert_called_once_with(7)
    coord.async_request_refresh.assert_awaited_once()
