"""Local MIoT client for ijai-family vacuums (synchronous; wrap in executor under HA)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from miio import MiotDevice

from .spec.registry import card_baseline_gaps, get_profile
from .spec.types import CoreCapability, MapCapability, ModelProfile, consumable_life_props

_LOGGER = logging.getLogger(__name__)


class DeviceCommunicationError(Exception):
    """Raised when a required device property cannot be read."""


# Length range for the wifi serial that seeds the map AES key. Firmware isn't
# consistent here — v3 reports 19, the old code only allowed 18 or 20 — so this
# is a range rather than a whitelist (issue #4).
_WIFI_SN_MIN_LEN = 16
_WIFI_SN_MAX_LEN = 24


def _is_wifi_sn(value: str) -> bool:
    return _WIFI_SN_MIN_LEN <= len(value) <= _WIFI_SN_MAX_LEN and value.isupper()


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
    door_state_raw: int | None
    main_brush_life: int | None
    side_brush_life: int | None
    filter_life: int | None
    mop_life: int | None
    dust_bag_life: int | None
    detergent_life: int | None
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
    def _batch_get(self, props: list) -> dict:
        """Batch-read MIoT props in one (or chunked) get_properties call.

        Returns {Prop: value|None}; properties whose device code is non-zero map
        to None.  Raises DeviceCommunicationError on network/protocol failure.
        The chunk size is capped at self.profile.max_properties when set — use
        this for devices that reject large batches (e.g. IJAI_CORE_LEGACY).
        """
        if not props:
            return {}
        batch_size = self.profile.max_properties
        miio_props = [
            {"did": f"{p.siid}-{p.piid}", "siid": p.siid, "piid": p.piid}
            for p in props
        ]
        try:
            raw = self._dev.get_properties(
                miio_props, property_getter="get_properties", max_properties=batch_size
            )
        except Exception as ex:  # noqa: BLE001
            raise DeviceCommunicationError(
                f"Property batch read failed ({len(props)} props): {ex}"
            ) from ex
        value_map: dict[tuple[int, int], object] = {
            (r["siid"], r["piid"]): r.get("value")
            for r in raw
            if isinstance(r, dict) and r.get("code", -1) == 0
        }
        return {p: value_map.get((p.siid, p.piid)) for p in props}

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
        # Lean core (decision 2026-06-25): clean-area/time are still NOT in
        # core, parked -> always None below. Consumable life: populated from
        # either capability shape via consumable_life_props() (life-level props
        # only — the left-time/hours props exist on the profile but aren't
        # polled, no consumer needs them yet). The ijai shape also carries the
        # door/box state (Vaschetta).
        cons = self.profile.consumables
        life = consumable_life_props(cons)
        door_state = getattr(cons, "door_state", None)
        # Batch all non-None props in one get_properties call (chunked when
        # profile.max_properties is set — e.g. IJAI_CORE_LEGACY devices).
        poll = [p for p in (
            c.status, c.battery, c.fault, c.fan_speed, c.water_level,
            c.mode, c.sweep_type, c.repeat, c.alarm, c.volume,
            *life.values(),
            door_state,
        ) if p is not None]
        vals = self._batch_get(poll)
        _raw = vals.get(c.status)
        try:
            raw = int(_raw)
        except (TypeError, ValueError) as ex:
            raise DeviceCommunicationError(
                f"Required property {c.status.siid}/{c.status.piid} read failed: "
                f"returned {_raw!r}"
            ) from ex
        return VacuumStatus(
            activity=c.status_map.get(raw, "idle"),
            raw_status=raw,
            battery=_as_int(vals.get(c.battery)),
            fault=_as_int(vals.get(c.fault)),
            fan_speed_raw=_as_int(vals.get(c.fan_speed)),
            water_level_raw=_as_int(vals.get(c.water_level)),
            mode_raw=_as_int(vals.get(c.mode)),
            sweep_type_raw=_as_int(vals.get(c.sweep_type)),
            repeat_raw=_as_int(vals.get(c.repeat)),
            alarm_raw=_as_int(vals.get(c.alarm)),
            volume_raw=_as_int(vals.get(c.volume)),
            door_state_raw=_as_int(vals.get(door_state)),
            main_brush_life=_as_int(vals.get(life.get("main_brush_life"))),
            side_brush_life=_as_int(vals.get(life.get("side_brush_life"))),
            filter_life=_as_int(vals.get(life.get("filter_life"))),
            mop_life=_as_int(vals.get(life.get("mop_life"))),
            dust_bag_life=_as_int(vals.get(life.get("dust_bag_life"))),
            detergent_life=_as_int(vals.get(life.get("detergent_life"))),
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
        # Prefer sweep.set-room-clean: it takes map/device room ids. The
        # vacuum.start-room-sweep action wants Mijia room ids (prop 2/10), so
        # map ids sent through it fail at device level (verified on v17
        # hardware, issue #7). Room-ids must stay a CSV string — the device
        # reads an integer as empty ids, which means a full global clean.
        preferred = self.room_clean_set_params(room_ids)
        if preferred is not None:
            action, params = preferred
            self._action(action, params)
            return
        fallback = self.room_clean_start_params(room_ids)
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

    # --- zone (area) cleaning -------------------------------------------
    def _point_zone(self):
        cap = self.profile.map
        if isinstance(cap, MapCapability):
            return cap.point_zone
        return None

    def zone_clean_action(self):
        """The set-zone-point action for this model, or None."""
        pz = self._point_zone()
        return None if pz is None else pz.set_zone_point

    def zone_clean_start_action(self):
        """The start-zone-clean action for this model, or None."""
        pz = self._point_zone()
        return None if pz is None else pz.start_zone_clean

    def zone_clean_params(self, x0: float, y0: float, x1: float, y1: float) -> list[str] | None:
        """set-zone-point params for a rectangle in the map's metre space.

        ijai expects a single ``"[x0,y0,x1,y1,1]"`` string in *millimetres*.
        The integration's map vector/camera coordinates are metres (see
        ``map_vector`` bounds/resolution), hence the x1000.
        """
        if self.zone_clean_action() is None:
            return None
        mm = [round(v * 1000) for v in (x0, y0, x1, y1)]
        return [f"[{mm[0]},{mm[1]},{mm[2]},{mm[3]},1]"]

    def _action_piid(self, action, values) -> dict:
        """Call an action whose MIoT spec declares piid-keyed inputs.

        The device rejects the bare-value form for these ("user ack timeout",
        -9999); each value must be sent as ``{"piid": p, "value": v}``, exactly
        as xiaomi_miot's ``in_params`` does. Actions without declared inputs
        fall through to the plain form.
        """
        piids = action.in_piids or (
            (action.in_piid,) if action.in_piid is not None else ()
        )
        if not piids:
            return self._action(action, values)
        params = [{"piid": p, "value": v} for p, v in zip(piids, values)]
        return self._action(action, params)

    def clean_zone(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """Start an area (zone) clean for the given metre rectangle.

        Two MIoT steps, both required (verified on ijai.vacuum.v19):

        1. ``set-zone-point`` (9/8) stores the rectangle (mm, piid-keyed string)
           and returns the map id/type/timestamp — on its own it does NOT start
           anything; the robot stays docked.
        2. ``start-zone-clean`` (9/3, no args) actually starts the clean.
        """
        action = self.zone_clean_action()
        params = self.zone_clean_params(x0, y0, x1, y1)
        if action is None or params is None:
            raise ValueError(f"{self.model} has no zone-clean capability")
        self._action_piid(action, params)
        start = self.zone_clean_start_action()
        if start is not None:
            self._action(start, [])

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
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("wifi_sn: siid 1/piid %s read failed: %s", piid, err)
                continue
            if isinstance(val, str) and _is_wifi_sn(val):
                return val
            _LOGGER.debug("wifi_sn: siid 1/piid %s value %r did not match expected shape", piid, val)
        try:
            raw = self._dev.get_property_by(7, 45)[0].get("value", "")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("wifi_sn: siid 7/piid 45 fallback read failed: %s", err)
            return None
        # The raw value is a bracketed list rendered as a string (e.g.
        # '[0,0,...,"XXXX"]'), not real JSON. Strip the outer brackets first
        # so the first and last elements do not retain a stray '[' / ']'
        # that would otherwise fail the isalnum() check below (serial in
        # last list position never matched, e.g. ijai.vacuum.v10).
        for part in str(raw).strip("[]").split(","):
            # The serial sits before an optional ";<uid>" suffix on siid 7/piid 45.
            p = part.replace('"', "").split(";")[0].strip()
            if _is_wifi_sn(p) and p.isalnum():
                return p
        _LOGGER.debug("wifi_sn: siid 7/piid 45 value %r had no matching serial part", raw)
        return None


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
