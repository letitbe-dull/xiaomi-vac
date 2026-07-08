"""Local MIoT client for ijai-family vacuums (synchronous; wrap in executor under HA)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from miio import MiotDevice

from .spec.registry import card_baseline_gaps, get_profile
from .spec.types import CoreCapability, MapCapability, ModelProfile

_LOGGER = logging.getLogger(__name__)


class DeviceCommunicationError(Exception):
    """Raised when a required device property cannot be read."""


# Lengths the firmware uses for the wifi serial that seeds the map AES key.
_WIFI_SN_LENS = (18, 20)


@dataclass
class VacuumStatus:
    activity: str
    raw_status: int
    battery: int | None
    fault: int | None
    fan_speed_raw: int | None
    water_level_raw: int | None
    mode_raw: int | None
    sweep_type_raw: int | None
    repeat_raw: int | None
    alarm_raw: int | None
    volume_raw: int | None
    main_brush_life: int | None
    side_brush_life: int | None
    filter_life: int | None
    mop_life: int | None
    clean_area: int | None
    clean_time: int | None


class IjaiVacuumDevice:
    """Thin wrapper over python-miio MiotDevice driven by a ModelProfile.

    Refuses to build for a model that has no profile or no runnable ``core``
    (rich-reference-only profiles, e.g. roidmi) — no blanket ijai fallback.
    """

    def __init__(self, host: str, token: str, model: str, timeout: int = 5):
        self.host = host
        self.token = token
        self.model = model
        profile = get_profile(model)
        if profile is None or profile.core is None:
            raise ValueError(f"{model} is not a runnable vacuum profile (no core)")
        gaps = card_baseline_gaps(profile)
        if gaps:
            raise ValueError(
                f"{model} does not satisfy the card baseline: {', '.join(gaps)}"
            )
        self.profile: ModelProfile = profile
        self.core: CoreCapability = profile.core
        self._dev = MiotDevice(host, token, timeout=timeout)

    # --- helpers ---------------------------------------------------------
    def _get(self, prop) -> object | None:
        if prop is None:
            return None
        try:
            return self._dev.get_property_by(prop.siid, prop.piid)[0].get("value")
        except Exception as ex:  # noqa: BLE001
            _LOGGER.debug("get %s/%s failed: %s", prop.siid, prop.piid, ex)
            return None

    def _get_required(self, prop) -> object:
        """Read a mandatory property; raises DeviceCommunicationError on any failure."""
        try:
            return self._dev.get_property_by(prop.siid, prop.piid)[0].get("value")
        except Exception as ex:  # noqa: BLE001
            raise DeviceCommunicationError(
                f"Required property {prop.siid}/{prop.piid} read failed: {ex}"
            ) from ex

    def _set(self, prop, value) -> None:
        if prop is None:
            raise ValueError(f"{self.model} does not support this property")
        self._dev.set_property_by(prop.siid, prop.piid, value)

    def _action(self, action, params=None) -> dict:
        if action is None:
            raise ValueError(f"{self.model} does not support this action")
        return self._dev.call_action_by(action.siid, action.aiid, params or [])

    # --- telemetry -------------------------------------------------------
    def status(self) -> VacuumStatus:
        c = self.core
        _raw = self._get_required(c.status)
        try:
            raw = int(_raw)
        except (TypeError, ValueError) as ex:
            raise DeviceCommunicationError(
                f"status property returned non-integer value: {_raw!r}"
            ) from ex
        # Lean core (decision 2026-06-25): consumable life and clean-area/time
        # are NOT in core; they're parked at launch -> always None here.
        return VacuumStatus(
            activity=c.status_map.get(raw, "idle"),
            raw_status=raw,
            battery=_as_int(self._get(c.battery)),
            fault=_as_int(self._get(c.fault)),
            fan_speed_raw=_as_int(self._get(c.fan_speed)),
            water_level_raw=_as_int(self._get(c.water_level)),
            mode_raw=_as_int(self._get(c.mode)),
            sweep_type_raw=_as_int(self._get(c.sweep_type)),
            repeat_raw=_as_int(self._get(c.repeat)),
            alarm_raw=_as_int(self._get(c.alarm)),
            volume_raw=_as_int(self._get(c.volume)),
            main_brush_life=None,
            side_brush_life=None,
            filter_life=None,
            mop_life=None,
            clean_area=None,
            clean_time=None,
        )

    # --- control ---------------------------------------------------------
    def start(self) -> None:
        self._action(self.core.start)

    def stop(self) -> None:
        self._action(self.core.stop)

    def pause(self) -> None:
        self._action(self.core.pause if self.core.pause is not None else self.core.stop)

    def return_home(self) -> None:
        self._action(self.core.charge)

    def locate(self) -> None:
        if self.core.locate is not None:
            self._action(self.core.locate)
        elif self.core.alarm is not None:
            self.set_alarm(True)
        else:
            raise ValueError(f"{self.model} has no locate capability")

    def set_fan_speed(self, preset: str) -> None:
        self._set(self.core.fan_speed, self.core.fan_speeds[preset])

    def set_water_level(self, preset: str) -> None:
        self._set(self.core.water_level, self.core.water_levels[preset])

    def set_mode(self, preset: str) -> None:
        self._set(self.core.mode, self.core.modes[preset])

    def set_sweep_type(self, preset: str) -> None:
        self._set(self.core.sweep_type, self.core.sweep_types[preset])

    def set_repeat(self, on: bool) -> None:
        self._set(self.core.repeat, 1 if on else 0)

    def set_alarm(self, on: bool) -> None:
        self._set(self.core.alarm, on)

    def set_volume(self, value: int) -> None:
        self._set(self.core.volume, int(value))

    def clean_segments(self, room_ids: list[int | str]) -> None:
        cap = self.profile.room_clean
        if cap is None:
            raise ValueError(f"{self.model} has no room-clean capability")
        start = self.room_clean_start_params(room_ids)
        if start is not None:
            action, params = start
            self._action(action, params)
            return
        fallback = self.room_clean_set_params(room_ids)
        if fallback is not None:
            action, params = fallback
            self._action(action, params)
            return
        raise ValueError(f"{self.model} has no usable room-clean action")

    def room_clean_start_params(self, room_ids: list[int | str]) -> tuple[object, list] | None:
        """Return the direct room-clean action and params, when available."""
        cap = self.profile.room_clean
        if cap is None or cap.start is None:
            return None
        return cap.start, [",".join(str(r) for r in room_ids)]

    def room_clean_set_params(self, room_ids: list[int | str]) -> tuple[object, list] | None:
        """Return the set-room-clean action and params, when available."""
        cap = self.profile.room_clean
        if not (
            cap is not None
            and cap.set_room_clean is not None
            and cap.clean_room_ids is not None
            and cap.clean_room_mode is not None
            and cap.clean_room_oper is not None
        ):
            return None
        values = {
            cap.clean_room_mode.piid: 0,  # global/all rooms mode
            cap.clean_room_oper.piid: 1,  # start
            cap.clean_room_ids.piid: ",".join(str(r) for r in room_ids),
        }
        return cap.set_room_clean, [values[piid] for piid in cap.set_room_clean.in_piids]

    # --- maps ------------------------------------------------------------
    def map_list(self) -> list[dict]:
        """Return [{'name', 'id', 'cur'}...] via get-map-list action.

        List-style maps only (ijai/viomi). dreame's blob map is a different shape
        (DreameMapCapability) with no map list — its decode is not yet implemented.

        The output piid varies by profile (ijai: siid 10/piid 4; viomi v12/v13/v15:
        siid 7/piid 11; viomi v45: siid 10/piid 4) — read from the profile's own
        `get_map_list.out_piids` rather than hardcoding ijai's value.
        """
        cap = self.profile.map
        if not isinstance(cap, MapCapability) or cap.get_map_list is None:
            return []
        out_piid = cap.get_map_list.out_piids[0] if cap.get_map_list.out_piids else 4
        res = self._action(cap.get_map_list)
        for out in res.get("out", []):
            if out.get("piid") == out_piid:
                try:
                    payload = json.loads(out["value"])
                except (ValueError, KeyError):
                    return []
                # viomi v15's map-list is an array-of-arrays, not a list of dicts
                # (spec/profiles/viomi.py VIOMI_V15_MAP) — reject any shape whose
                # items aren't {"id": ...} dicts rather than crashing fetch_all's
                # m.get("cur")/m["id"] reads downstream.
                if not isinstance(payload, list) or not all(
                    isinstance(m, dict) and "id" in m for m in payload
                ):
                    _LOGGER.debug("map-list payload has unsupported shape: %r", payload)
                    return []
                return payload
        return []

    def request_map_upload(self, map_id: int) -> dict:
        """Trigger a fresh upload for a map-list map; returns raw out."""
        cap = self.profile.map
        if not isinstance(cap, MapCapability):
            raise ValueError(f"{self.model} has no map-upload capability")
        actions = []
        if cap.get_map_list is not None and cap.upload_by_mapid_ii is not None:
            actions.append(cap.upload_by_mapid_ii)
        if cap.upload_by_mapid is not None:
            actions.append(cap.upload_by_mapid)
        elif cap.upload_by_mapid_ii is not None:
            actions.append(cap.upload_by_mapid_ii)
        if not actions:
            raise ValueError(f"{self.model} has no map-upload capability")
        last_error: Exception | None = None
        for action in actions:
            try:
                return self._action(action, [int(map_id)])
            except Exception as err:  # noqa: BLE001
                last_error = err
                if action is not actions[-1]:
                    _LOGGER.debug(
                        "map upload action %s/%s failed, trying fallback: %s",
                        action.siid, action.aiid, err,
                    )
                    continue
                raise
        raise ValueError(f"{self.model} map-upload failed: {last_error}")

    def set_current_map(self, map_id: int) -> None:
        """Switch the vacuum's active map (multi-map devices)."""
        cap = self.profile.map
        if not isinstance(cap, MapCapability) or cap.set_current_map is None:
            raise ValueError(f"{self.model} has no map-switch capability")
        self._action(cap.set_current_map, [int(map_id)])

    def get_mac(self) -> str | None:
        """Device MAC (used in the map AES key). From local miIO info()."""
        try:
            return self._dev.info().mac_address
        except Exception:  # noqa: BLE001
            return None

    def get_wifi_sn(self, user_id: str | None = None) -> str | None:
        """Serial used to seed the map AES key (siid 1, piid 5 on 2022+ models)."""
        for piid in (5, 3):
            try:
                val = self._dev.get_property_by(1, piid)[0].get("value")
            except Exception:  # noqa: BLE001
                continue
            if isinstance(val, str) and len(val) in _WIFI_SN_LENS and val.isupper():
                return val
        try:
            raw = self._dev.get_property_by(7, 45)[0].get("value", "")
        except Exception:  # noqa: BLE001
            return None
        for part in str(raw).split(","):
            # The serial sits before an optional ";<uid>" suffix on siid 7/piid 45.
            p = part.replace('"', "").split(";")[0]
            if len(p) in _WIFI_SN_LENS and p.isalnum() and p.isupper():
                return p
        return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
