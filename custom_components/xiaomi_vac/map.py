"""Fetch + decrypt + parse a vacuum map into a PNG and the plug-and-play
attribute contract the card consumes. Synchronous; run in an executor."""
from __future__ import annotations

import io
import logging
import zlib
from dataclasses import dataclass, field

from PIL import Image, ImageChops
from vacuum_map_parser_base.config.color import ColorsPalette
from vacuum_map_parser_base.config.drawable import Drawable
from vacuum_map_parser_base.config.image_config import ImageConfig
from vacuum_map_parser_base.config.size import Sizes
from vacuum_map_parser_ijai.map_data_parser import IjaiMapDataParser

from . import map_vector
from .cloud.connector import XiaomiCloud
from .map_parsers import brand_of, has_ijai_grid, make_parser, unpack_kwargs

_LOGGER = logging.getLogger(__name__)


def _patch_parse_rooms() -> None:
    """Work around an upstream crash on NON-ACTIVE maps (multi-map).

    `IjaiMapDataParser._parse_rooms` looks up the entry in `mapInfo` whose
    `mapHeadId` equals the active map's, purely to log its name. On a stored,
    non-active map that id matches nothing, so `current_map` is left unbound and
    the method raises `UnboundLocalError` BEFORE the room-naming loop runs —
    killing the whole parse. The naming loop itself reads `roomDataInfo` and does
    not need `current_map` at all, so we drop in a version that guards the lookup.
    Pinned dep (vacuum-map-parser-ijai==0.1.1); bug still present in 0.1.1,
    revisit if upstream fixes it.
    """
    parser_cls = IjaiMapDataParser

    @staticmethod
    def _parse_rooms(map_data_rooms: dict) -> None:
        rm = parser_cls.robot_map
        map_id = rm.mapHead.mapHeadId
        current_map = next((m for m in rm.mapInfo if m.mapHeadId == map_id), None)
        if current_map is not None:
            _LOGGER.debug("map#%d: %s", current_map.mapHeadId, current_map.mapName)
        for r in rm.roomDataInfo:
            if map_data_rooms is not None and r.roomId in map_data_rooms:
                map_data_rooms[r.roomId].name = r.roomName
                map_data_rooms[r.roomId].pos_x = r.roomNamePost.x
                map_data_rooms[r.roomId].pos_y = r.roomNamePost.y

    parser_cls._parse_rooms = _parse_rooms


_patch_parse_rooms()

_DRAWABLES = [
    Drawable.PATH, Drawable.CHARGER, Drawable.VACUUM_POSITION,
    Drawable.ROOM_NAMES, Drawable.NO_GO_AREAS, Drawable.VIRTUAL_WALLS,
]


class SessionExpired(Exception):
    """The cloud session no longer returns a map URL (token likely expired)."""


@dataclass
class MapResult:
    image_png: bytes
    attributes: dict
    vector: dict  # ACTIVE map's grid + vector overlays (back-compat)
    # All maps the device lists, each a vector dict tagged with map_id/map_name/
    # active. Always contains at least the active map; extra entries appear only
    # when the device actually has more than one map.
    maps: list = field(default_factory=list)


def _od(obj):
    return obj.as_dict() if obj is not None else None


def _autocrop(img: Image.Image, pad: int = 20) -> tuple[Image.Image, int, int]:
    """Crop the uniform background margin off the map.

    Returns the cropped image and the (left, top) offset removed, so callers
    can shift pixel-space data (calibration points) to keep it aligned.
    """
    rgb = img.convert("RGB")
    bg = Image.new("RGB", rgb.size, rgb.getpixel((0, 0)))
    bbox = ImageChops.difference(rgb, bg).getbbox()
    if not bbox:
        return img, 0, 0
    left = max(0, bbox[0] - pad)
    top = max(0, bbox[1] - pad)
    right = min(img.width, bbox[2] + pad)
    bottom = min(img.height, bbox[3] + pad)
    return img.crop((left, top, right, bottom)), left, top


class MapFetcher:
    """Owns the map parser; pulls the active map and builds the contract."""

    def __init__(self, cloud: XiaomiCloud, *, server: str, user_id: str,
                 device_id: str, model: str, mac: str, wifi_sn: str):
        self._cloud = cloud
        self._server = server
        self._user_id = str(user_id)
        self._device_id = str(device_id)
        self._model = model
        self._mac = mac
        self._wifi_sn = wifi_sn
        self._brand = brand_of(model)
        self._parser = make_parser(
            self._brand, model, ColorsPalette(), Sizes(), _DRAWABLES, ImageConfig(), []
        )
        # Inputs for parser.unpack_map; the brand decides which are used. enckey
        # (encrypted dreame maps) has no verified source yet -> None (best-effort).
        self._unpack_kw = unpack_kwargs(
            self._brand, wifi_sn=self._wifi_sn, owner_id=self._user_id,
            device_id=self._device_id, model=self._model, device_mac=self._mac,
        )
        self._ijai_grid = has_ijai_grid(self._brand)
        # Non-active map vectors are static; fetch each at most once per fetcher
        # lifetime (keyed by map id, caches None on failure) so the timer never
        # spams the cloud. Cleared on integration reload.
        self._inactive_cache: dict[int, dict | None] = {}

    def fetch_all(self, maps_meta: list[dict] | None = None) -> MapResult | None:
        """Fetch the active map plus best-effort vectors for any OTHER maps the
        device lists. Returns the active MapResult with `.maps` populated.

        `maps_meta` is `device.map_list()` output ([{name,id,cur}, ...]). With a
        single-map device this is just the active map and no cloud writes happen.
        """
        active = self.fetch("0")
        if active is None:
            return None
        meta = maps_meta or []
        active_meta = next((m for m in meta if m.get("cur")), None)
        active_id = active.vector.get("map_id")
        entries = [{**active.vector, "map_id": active_id,
                    "map_name": (active_meta or {}).get("name"), "active": True}]
        for m in meta:
            if m.get("cur") or m.get("id") is None:
                continue
            vec = self._inactive_vector(int(m["id"]), active_id)
            if vec:
                entries.append({**vec, "map_id": m["id"],
                                "map_name": m.get("name"), "active": False})
        active.maps = entries
        return active

    def _inactive_vector(self, map_id: int, active_id) -> dict | None:
        """Best-effort vector for a non-active map. UNPROVEN: the obj_name a
        stored map lands at after an upload-by-id isn't confirmed (only ever had
        one map to test), so this asks the cloud to upload it then probes a few
        candidate slots, accepting only a blob whose map_id differs from the
        active one. Safe to fail; result (incl. None) is cached."""
        if map_id in self._inactive_cache:
            return self._inactive_cache[map_id]
        vec = None
        try:
            self._cloud.cloud_action(self._server, self._device_id, 10, 2, [map_id])
            for slot in ("1", str(map_id)):
                url = self._cloud.map_url(self._server, self._device_id, slot)
                raw = self._cloud.download(url) if url else None
                if not raw or raw == b"[]":
                    continue
                v = self._decode_vector(raw)
                if v and v.get("map_id") not in (None, active_id) and v.get("rooms"):
                    vec = v
                    break
        except Exception as ex:  # noqa: BLE001
            _LOGGER.debug("inactive map %s fetch failed: %s", map_id, ex)
        self._inactive_cache[map_id] = vec
        return vec

    def _decode_vector(self, raw: bytes) -> dict | None:
        """Decrypt+parse a blob to just the vector contract (no PNG render)."""
        try:
            unpacked = self._parser.unpack_map(raw, **self._unpack_kw)
            md = self._parser.parse(unpacked)
            return map_vector.vector_map(md, unpacked, ijai_grid=self._ijai_grid)
        except Exception as ex:  # noqa: BLE001
            _LOGGER.debug("inactive map decode failed: %s", ex)
            return None

    def fetch(self, map_name: str = "0") -> MapResult | None:
        url = self._cloud.map_url(self._server, self._device_id, map_name)
        if not url:
            # No URL usually means the cloud session expired; let the
            # coordinator try a token refresh.
            raise SessionExpired()
        raw = self._cloud.download(url)
        if not raw:
            _LOGGER.warning("Map download failed")
            return None

        try:
            unpacked = self._parser.unpack_map(raw, **self._unpack_kw)
            md = self._parser.parse(unpacked)
            vector = map_vector.vector_map(md, unpacked, ijai_grid=self._ijai_grid)
        except Exception as ex:  # noqa: BLE001
            # A corrupt / incomplete map (e.g. after a vacuum wifi reset) fails
            # to decrypt or parse. Don't crash the coordinator over it.
            _LOGGER.warning(
                "Could not decode the vacuum map (corrupt or unsupported map "
                "data — try running a fresh clean): %s", ex)
            return None
        if md.image is None or md.image.is_empty:
            _LOGGER.debug("Parsed map is empty")
            return None

        cropped, off_x, off_y = _autocrop(md.image.data)
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")

        # Shift calibration map-pixels by the cropped-away margin so the card
        # overlay still maps vacuum coordinates to the right place.
        calibration = md.calibration() or []
        for cp in calibration:
            cp["map"]["x"] -= off_x
            cp["map"]["y"] -= off_y

        attributes = {
            "calibration_points": calibration,
            "rooms": [{"id": rid, **r.as_dict()} for rid, r in (md.rooms or {}).items()],
            "charger": _od(md.charger),
            "vacuum_position": _od(md.vacuum_position),
            "vacuum_room": md.vacuum_room,
            "vacuum_room_name": md.vacuum_room_name,
            "zones": [_od(z) for z in (md.zones or [])],
            "no_go_areas": [_od(a) for a in (md.no_go_areas or [])],
            "no_mopping_areas": [_od(a) for a in (md.no_mopping_areas or [])],
            "walls": [_od(w) for w in (md.walls or [])],
            "image_width": cropped.width,
            "image_height": cropped.height,
            "map_name": map_name,
        }
        return MapResult(image_png=buf.getvalue(), attributes=attributes, vector=vector)
