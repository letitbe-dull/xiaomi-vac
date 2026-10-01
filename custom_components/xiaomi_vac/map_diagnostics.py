"""Diagnostic record of one map fetch cycle. No homeassistant imports."""
from __future__ import annotations

from dataclasses import dataclass, field, fields


@dataclass
class SlotAttempt:
    """What one MapFetcher.fetch() call did for one cloud upload slot."""

    slot: str
    url_obtained: bool = False
    blob_bytes: int | None = None
    outcome: str = "incomplete"

    def as_dict(self) -> dict:
        return {
            "slot": self.slot,
            "url_obtained": self.url_obtained,
            "blob_bytes": self.blob_bytes,
            "outcome": self.outcome,
        }


@dataclass
class UploadRequest:
    """A map-upload request sent ahead of a cycle: route "cloud" or "local", and whether its reply was ok."""

    route: str
    ok: bool


def describe_map_capability(cap) -> dict | None:
    """Declared MIoT ids of a profile's map capability, or None when it has none.

    @param cap: the profile's MapCapability (duck-typed dataclass), or None.
    @returns: {"service": siid, "declared": {field: "siid.piid"|"siid.aiid"|"yes"}}.
    """
    if cap is None or isinstance(cap, type) or not hasattr(type(cap), "__dataclass_fields__"):
        return None
    declared = {}
    for f in fields(cap):
        value = getattr(cap, f.name)
        if f.name == "service" or value is None:
            continue
        if hasattr(value, "piid"):
            declared[f.name] = f"{value.siid}.{value.piid}"
        elif hasattr(value, "aiid"):
            declared[f.name] = f"{value.siid}.{value.aiid}"
        else:
            declared[f.name] = "yes"
    return {"service": cap.service, "declared": declared}


def resolve_active_id(
    decoded: list, active_meta: dict | None, mqtt_active_id: int | None,
    has_map_list: bool, maps_meta: list[dict], *, single_map_id: int,
) -> tuple[int | None, str | None]:
    """Which map this cycle's data belongs to, and the trust step that decided.

    @param decoded: MapResults decoded this cycle (each with `.map_id`).
    @param active_meta: the map-list entry flagged "cur", if any.
    @param mqtt_active_id: last MQTT-signalled curMapId, if any.
    @param has_map_list: whether the profile has a map-list capability at all.
    @param maps_meta: this cycle's map-list read.
    @param single_map_id: cache key for a profile with exactly one physical map.
    @returns: (map_id, step) with step one of "blob", "map_list", "mqtt",
        "single_map"; (None, None) when nothing resolved.
    """
    for r in decoded:
        if r.map_id is not None:
            return r.map_id, "blob"
    if active_meta and active_meta.get("id") is not None:
        try:
            return int(active_meta["id"]), "map_list"
        except (TypeError, ValueError):
            pass
    if mqtt_active_id is not None:
        return mqtt_active_id, "mqtt"
    if not has_map_list and not maps_meta:
        return single_map_id, "single_map"
    return None, None


@dataclass
class MapCycleRecord:
    """What the most recent map coordinator cycle did. Most-recent-wins."""

    parser_key: str
    map_capability: dict | None
    slots: list[SlotAttempt] = field(default_factory=list)
    resolved_map_id: int | None = None
    resolved_by: str | None = None
    served: str = "none"
    upload_request: UploadRequest | None = None

    def set_served(self, *, decoded: bool, have_result: bool) -> None:
        """Record whether the cycle served live data, cache, or nothing.

        @param decoded: whether any slot decoded a map this cycle.
        @param have_result: whether a map was available to serve at all.
        """
        self.served = ("live" if decoded else "cache") if have_result else "none"

    def as_dict(self) -> dict:
        return {
            "parser_key": self.parser_key,
            "map_capability": self.map_capability,
            "url_obtained": any(s.url_obtained for s in self.slots),
            "session_expired": bool(self.slots) and not any(s.url_obtained for s in self.slots),
            "rendered": any(s.outcome == "rendered" for s in self.slots),
            "slots": [s.as_dict() for s in self.slots],
            "resolved_map_id": self.resolved_map_id,
            "resolved_by": self.resolved_by,
            "served": self.served,
            "upload_request_sent": self.upload_request is not None,
            "upload_request_route": self.upload_request.route if self.upload_request else None,
            "upload_request_ok": self.upload_request.ok if self.upload_request else None,
        }
