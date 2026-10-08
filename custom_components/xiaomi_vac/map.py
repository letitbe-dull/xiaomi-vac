"""Fetch + decrypt + parse a vacuum map into a PNG and the plug-and-play
attribute contract the card consumes. Synchronous; run in an executor."""
from __future__ import annotations

import hashlib
import io
import json
import logging
import urllib.parse
import zlib
from dataclasses import dataclass, field

from PIL import Image, ImageChops
from vacuum_map_parser_base.config.color import ColorsPalette
from vacuum_map_parser_base.config.drawable import Drawable
from vacuum_map_parser_base.config.image_config import ImageConfig
from vacuum_map_parser_base.config.size import Sizes
from vacuum_map_parser_ijai.map_data_parser import IjaiMapDataParser

from . import map_vector
from .cloud.connector import CloudUnreachable, XiaomiCloud
from .map_diagnostics import SlotAttempt
from .map_parsers import (
    dreame_decrypt_cloud_blob,
    dreame_extract_enckey,
    has_ijai_grid,
    has_json_grid,
    make_parser,
    map_url_endpoint,
    overlay_units_per_metre,
    unpack_kwargs,
)
from .xiaomi_map_overlays import draw_overlays, parse_carpets, parse_path

# MIoT property that carries `<object_path>,<enckey>` for cloud-encrypted dreame maps.
# Verified against Tasshack's dreame-vacuum 2026-07-03 (siid=6/piid=3 = OBJECT_NAME).
_DREAME_ENCKEY_SIID = 6
_DREAME_ENCKEY_PIID = 3

_LOGGER = logging.getLogger(__name__)

_RAW_ZLIB_MODELS = frozenset({"ijai.vacuum.v2"})


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
    # Physical map id (mapHeadId) this render belongs to; None when the brand's
    # blob carries no id of its own (non-ijai — the coordinator's map-list
    # metadata is the id source of truth in that case).
    map_id: int | None = None
    # sha256 of the pre-render unpacked map bytes; lets the coordinator's cache
    # skip rewriting storage when a poll yields byte-identical content.
    content_hash: str | None = None
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
                 device_id: str, model: str, mac: str, wifi_sn: str, parser_brand: str):
        self._cloud = cloud
        self._server = server
        self._user_id = str(user_id)
        self._device_id = str(device_id)
        self._model = model
        self._mac = mac
        self._wifi_sn = wifi_sn
        self._brand = parser_brand
        self._parser = make_parser(
            self._brand, model, ColorsPalette(), Sizes(), _DRAWABLES, ImageConfig(), []
        )
        # Inputs for parser.unpack_map; the brand decides which are used.
        self._unpack_kw = unpack_kwargs(
            self._brand, wifi_sn=self._wifi_sn, owner_id=self._user_id,
            device_id=self._device_id, model=self._model, device_mac=self._mac,
        )
        self._endpoint = map_url_endpoint(self._brand)
        self._ijai_grid = has_ijai_grid(self._brand)
        # True for the xiaomi JSON-map family: room contours are traced from
        # the labelled pixel grid inside its decrypted JSON payload.
        self._json_grid = has_json_grid(self._brand)
        # Divisor turning this brand's overlay coords into the metres the card
        # contract declares (1.0 for every brand but the xiaomi JSON family).
        self._overlay_units = overlay_units_per_metre(self._brand)
        # Dreame cloud enckey polled from siid=6/piid=3 on first fetch; None for
        # unencrypted models or until the property is successfully read.
        self._enckey: str | None = None
        self._enckey_polled = False
        # What the most recent fetch() call did; overwritten by every call.
        self.last_attempt: SlotAttempt | None = None

    def _get_dreame_enckey(self) -> str | None:
        """Poll siid=6/piid=3 for the dreame cloud map encryption key."""
        resp = self._cloud.cloud_get_prop(
            self._server, self._device_id, _DREAME_ENCKEY_SIID, _DREAME_ENCKEY_PIID)
        try:
            val = resp["result"][0]["value"]
            return dreame_extract_enckey(val)
        except (TypeError, KeyError, IndexError):
            return None

    def _unpack(self, raw: bytes) -> bytes:
        """Brand-dispatch: return decompressed map bytes ready for parser.parse().

        dreame with enckey: if the parser has a model-specific IV, delegate to
        parser.unpack_map (it applies AES-CBC with that IV). Otherwise use the
        Tasshack zero-IV chain via dreame_decrypt_cloud_blob.
        xiaomi: bypasses parser.unpack_map (vacuum_map_parser_xiaomi's own
        decrypt() is broken for this whole model family, see
        xiaomi_json_decrypt.py) and decrypts locally instead. Returns a JSON
        *string*, not bytes, same contract as the upstream decrypt() this
        replaces (parser.parse() only accepts str or dict).
        Raw zlib Protobuf is accepted only for explicitly verified models.
        All other paths go through parser.unpack_map normally.
        """
        if self._model in _RAW_ZLIB_MODELS and raw.startswith(b"\x78\x9c"):
            unpacked = zlib.decompress(raw)
            if not unpacked.startswith(b"\x08"):
                raise ValueError("raw zlib map is not an ijai Protobuf frame")
            return unpacked
        if self._brand == "dreame" and self._enckey is not None:
            from vacuum_map_parser_dreame.map_data_parser import DreameMapDataParser
            if DreameMapDataParser.IVs.get(self._model) is not None:
                return self._parser.unpack_map(raw, enckey=self._enckey)
            return dreame_decrypt_cloud_blob(raw, self._enckey)
        if self._brand == "xiaomi":
            from .xiaomi_json_decrypt import decrypt_xiaomi_json_map
            return decrypt_xiaomi_json_map(raw, self._model, self._device_id)
        if self._brand == "viomi" and raw.startswith(b"\x1f\x8b"):
            # Some viomi profiles (confirmed on v22) upload a gzip-wrapped blob
            # instead of the raw zlib stream ViomiMapDataParser.unpack_map()
            # expects — it calls zlib.decompress(raw) with default wbits, which
            # only understands a zlib header (0x78..), not gzip's (0x1f 0x8b),
            # and fails with "incorrect header check". Unwrap it ourselves.
            import gzip
            return gzip.decompress(raw)
        return self._parser.unpack_map(raw, **self._unpack_kw)

    def _parse_viomi_json_trailer(self, unpacked: bytes):
        """viomi.vacuum.v22 (confirmed; likely siblings) doesn't use the
        binary feature-flag sections ViomiMapDataParser.parse() expects.
        Instead it writes a raw one-byte-per-pixel occupancy grid (the same
        pixel encoding ViomiImageParser already understands — 0x00 outside,
        0xff wall, 0x7f undiscovered) immediately followed by a JSON object
        carrying width/height/mapId/x_min/y_min/resolution, the charge dock
        and robot pose, and virtual walls/no-go zones ("area").

        Confirmed byte-for-byte on a real capture: header_len + width*height
        lands exactly on the JSON's opening '{', and the JSON's one "area"
        entry / absent room list matched the live device's one configured
        virtual wall and zero rooms. Returns None (falls back to the normal
        binary parser) if this frame doesn't look like this format.
        """
        brace = unpacked.find(b"{")
        if brace < 0:
            return None
        try:
            meta = json.loads(unpacked[brace:])
        except (ValueError, UnicodeDecodeError):
            return None
        width, height = meta.get("width"), meta.get("height")
        if not (
            isinstance(width, int) and isinstance(height, int)
            and width > 0 and height > 0
        ):
            return None
        header_len = brace - width * height
        # A real header here is a handful of bytes (feature flags + a magic/
        # map-id word in every sample seen so far); anything larger means the
        # '{' we found is coincidental and this isn't our format after all.
        if not (0 <= header_len <= 64):
            return None

        from vacuum_map_parser_base.map_data import (
            ImageData, MapData, Path, Point, Room, Wall,
        )
        from vacuum_map_parser_viomi.parsing_buffer import ParsingBuffer

        pixel_grid = unpacked[header_len:brace]
        buf = ParsingBuffer("image", pixel_grid, 0, len(pixel_grid))
        image, _rooms_raw, cleaned_areas, cleaned_areas_layer = (
            self._parser._image_parser.parse(buf, width, height)  # noqa: SLF001
        )
        if image is None:
            return None

        resolution = float(meta.get("resolution") or 0.05)
        x_min = float(meta.get("x_min") or 0.0)
        y_min = float(meta.get("y_min") or 0.0)

        # map_vector.vector_map() emits charger/vacuum/walls/etc. by dividing
        # by self._overlay_units, which is 1.0 for brand "viomi" (the binary
        # protocol's own _parse_position already returns metres) — so every
        # Point/Wall built below must be in METRES too, not this device's
        # native millimetres, or the card receives e.g. charger.x = -449
        # (metres!) instead of -0.449 and renders a wildly out-of-frame,
        # giant icon with nothing else in the viewport (the bug this fixes).
        mm_to_m = 1000.0

        def to_image(p: Point) -> Point:
            # p is already in metres (see mm_to_m above); x_min/y_min/
            # resolution (also metres) place that frame on the pixel grid.
            # Distinct from (and not to be confused with) the binary-protocol
            # profiles' fixed *20+400 transform, a different coordinate
            # convention for a different frame.
            return Point((p.x - x_min) / resolution, (p.y - y_min) / resolution)

        map_data = MapData(0, 1)
        map_data.image = ImageData(
            width * height, 0, 0, height, width,
            self._parser._image_config, image, to_image,  # noqa: SLF001
            additional_layers={Drawable.CLEANED_AREA: cleaned_areas_layer},
        )
        # "autoArea" is this device's room list: [{"id","name","pos":[x,y]}],
        # millimetres. It's a label ANCHOR POINT only — no boundary/polygon —
        # so each Room gets a zero-area bbox at that point. That's enough for
        # the card's name label, and more importantly it feeds every room
        # position into the card's auto-fit viewBox (map_vector's bbox-less
        # fallback path builds one rectangle per room from x0/y0/x1/y1), so
        # the view spans the whole explored house instead of just the
        # charger — the only point it had with zero rooms configured.
        auto_area = meta.get("autoArea") or []
        raw_ids = [
            e.get("id") for e in auto_area if isinstance(e.get("id"), int)
        ]
        # Map whatever this device's lowest id actually is onto 10 (the start
        # of the card's room/colour band) rather than assuming it's always 1.
        room_id_offset = (10 - min(raw_ids)) if raw_ids else 0

        rooms = {}
        for entry in auto_area:
            pos = entry.get("pos")
            rid = entry.get("id")
            if not (isinstance(pos, list) and len(pos) == 2 and isinstance(rid, int)):
                continue
            rx, ry = pos[0] / mm_to_m, pos[1] / mm_to_m
            # Custom (renamed) room names come through URL-percent-encoded
            # for any non-ASCII character (confirmed: "Étkező" arrived as
            # "%C3%89tkez%C5%91") — the auto-generated "RoomN" placeholders
            # don't need it, but decoding is a no-op for plain ASCII anyway.
            name = entry.get("name")
            if isinstance(name, str):
                name = urllib.parse.unquote(name)
            # This device's native room ids are 1-9 — below the 10-59 band
            # both ViomiImageParser's own raster colouring and the card's
            # `_roomRaster`/`_mapSVG` hard-code for "this pixel/id is a room".
            # Card code looks up a room's tint by indexing `rooms` with the
            # SAME id the pixel grid carries (`idIndex[lab]` against `lab`
            # read straight off a grid cell), so the shift has to be applied
            # once, consistently, everywhere that id is used — the Room
            # itself, the dict key, the grid pixel value AND the traced
            # chain's id all have to agree or the colour lookup silently
            # misses and every room falls back to the same tint (shape
            # renders, but as one flat colour — exactly what an id-only
            # shift on the grid, not on the exposed room id, produced).
            card_id = rid + room_id_offset
            rooms[card_id] = Room(rx, ry, rx, ry, card_id, name=name,
                                   pos_x=rx, pos_y=ry)
        map_data.rooms = rooms
        map_data.cleaned_rooms = cleaned_areas

        # The pixel grid encodes room membership too — confirmed values 1..9
        # (exactly this map's room ids) alongside 0x00/0x7f/0xff (outside/
        # undiscovered/wall) once rooms exist. Trace outlines with the same
        # cell-mask tracer `extract_json_grid` uses, so the card gets real
        # room fills/outlines instead of only the label points above.
        # Stashed on map_data rather than threaded through vector_map's fixed
        # ijai_grid/json_grid dispatch, which has no slot for "grid already
        # computed by the caller" — fetch() picks this up below.
        if rooms:
            remapped = bytearray(pixel_grid)
            masks: dict[int, set] = {}
            for i, v in enumerate(pixel_grid):
                card_id = v + room_id_offset
                if card_id in rooms:
                    remapped[i] = card_id
                    masks.setdefault(card_id, set()).add((i % width, i // width))
            map_data._viomi_grid_extra = {  # noqa: SLF001
                "size": {"x": width, "y": height},
                "bounds": {
                    "minX": x_min, "minY": y_min,
                    "maxX": x_min + width * resolution,
                    "maxY": y_min + height * resolution,
                },
                "resolution": resolution,
                "grid_rle": map_vector._rle(bytes(remapped)),  # noqa: SLF001
                "room_chains": (
                    map_vector._chains_from_masks(masks) if masks else []  # noqa: SLF001
                ),
            }

        walls = []
        for entry in meta.get("area") or []:
            pts = entry.get("vertexs") or []
            if len(pts) == 2:
                (x0, y0), (x1, y1) = pts
                walls.append(Wall(
                    x0 / mm_to_m, y0 / mm_to_m, x1 / mm_to_m, y1 / mm_to_m
                ))
        map_data.walls = walls
        map_data.no_go_areas = []
        map_data.zones = []

        charger = meta.get("chargeHandlePos")
        if isinstance(charger, list) and len(charger) == 2:
            map_data.charger = Point(charger[0] / mm_to_m, charger[1] / mm_to_m)
        robot = meta.get("robotPos")
        if isinstance(robot, list) and len(robot) == 2:
            map_data.vacuum_position = Point(
                robot[0] / mm_to_m, robot[1] / mm_to_m, meta.get("robotPhi")
            )

        # "posArray" is the travelled path: a JSON array *string* (double-
        # encoded, like "posArray" itself being a string value) of [x, y]
        # millimetre pairs, "pathSize" entries long.
        pos_array = meta.get("posArray")
        if isinstance(pos_array, str):
            try:
                points = json.loads(pos_array)
            except (ValueError, TypeError):
                points = None
            if isinstance(points, list) and points:
                path_points = [
                    Point(p[0] / mm_to_m, p[1] / mm_to_m)
                    for p in points
                    if isinstance(p, list) and len(p) == 2
                ]
                if path_points:
                    map_data.path = Path(len(path_points), 1, 0, [path_points])
        return map_data

    def fetch(self, slot: str = "0") -> MapResult | None:
        """Fetch + decrypt + parse one cloud upload slot ("0" or "1").

        Returns None for anything that isn't a readable render: an
        undecryptable ("Key B") blob, a corrupt/incomplete one, or an empty
        map. Raises SessionExpired when the cloud won't even hand back a URL
        (token likely dead) — the coordinator handles renewal.
        """
        if self._brand == "dreame" and not self._enckey_polled:
            self._enckey = self._get_dreame_enckey()
            self._enckey_polled = True
            _LOGGER.debug("dreame enckey poll: %s",
                          "found" if self._enckey else "not found (unencrypted or unavailable)")
        attempt = self.last_attempt = SlotAttempt(slot=slot)
        try:
            url = self._cloud.map_url(self._server, self._device_id, slot, self._endpoint)
        except CloudUnreachable:
            attempt.outcome = "unreachable"
            raise
        attempt.url_obtained = bool(url)
        if not url:
            # No URL usually means the cloud session expired; let the
            # coordinator try a token refresh.
            attempt.outcome = "no_url"
            raise SessionExpired()
        raw = self._cloud.download(url)
        attempt.blob_bytes = len(raw) if raw else 0
        if not raw:
            attempt.outcome = "empty_download"
            # Not per-slot actionable; the coordinator raises UpdateFailed when
            # every fallback (both slots + cache) comes up empty.
            _LOGGER.debug("Map download failed (slot %s)", slot)
            return None

        try:
            unpacked = self._unpack(raw)
        except Exception as ex:  # noqa: BLE001
            # Decrypt/decompress failed: a corrupt blob OR (routinely, per the
            # map-reliability plan) an undecryptable "Key B" blob at this slot.
            # Never crash the coordinator — it falls back to the other slot or
            # the cache. A stale dreame enckey also lands here: drop it so the
            # next fetch re-polls siid=6/piid=3.
            if self._brand == "dreame" and self._enckey is not None:
                _LOGGER.debug("dreame decrypt failed; will re-poll enckey next fetch: %s", ex)
                self._enckey = None
                self._enckey_polled = False
            _LOGGER.debug("Could not decrypt map at slot %s (starts %r): %s", slot, raw[:16], ex)
            attempt.outcome = "undecryptable"
            return None
        carpets = parse_carpets(unpacked) if self._brand == "xiaomi" else []
        path_segments = parse_path(unpacked) if self._brand == "xiaomi" else []
        try:
            md = None
            if self._brand == "viomi":
                md = self._parse_viomi_json_trailer(unpacked)
            if md is None:
                md = self._parser.parse(unpacked)
            vector = map_vector.vector_map(
                md, unpacked, ijai_grid=self._ijai_grid,
                json_grid=self._json_grid,
                units_per_metre=self._overlay_units,
                carpets=carpets,
                path_segments=path_segments,
            )
            grid_extra = getattr(md, "_viomi_grid_extra", None)
            if grid_extra is not None:
                vector.update(grid_extra)
        except Exception as ex:  # noqa: BLE001
            # Decrypted fine but the parser rejected the frame (corrupt or
            # unexpected layout). The key material is good — keep the enckey.
            import traceback
            _LOGGER.debug(
                "Parser rejected map frame at slot %s: %r\n%s",
                slot, ex, traceback.format_exc(),
            )
            attempt.outcome = "parse_rejected"
            return None
        if md.image is None or md.image.is_empty:
            _LOGGER.debug("Parsed map at slot %s is empty", slot)
            attempt.outcome = "empty_render"
            return None

        cropped, off_x, off_y = _autocrop(md.image.data)
        calibration = md.calibration() or []
        cropped = draw_overlays(
            cropped, carpets, path_segments, calibration, off_x, off_y
        )
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")

        # Shift calibration map-pixels by the cropped-away margin so the card
        # overlay still maps vacuum coordinates to the right place.
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
        }
        attempt.outcome = "rendered"
        return MapResult(
            image_png=buf.getvalue(),
            attributes=attributes,
            vector=vector,
            map_id=vector.get("map_id"),
            # xiaomi's _unpack returns a str (JSON text), every other brand bytes.
            content_hash=hashlib.sha256(
                unpacked.encode("utf-8") if isinstance(unpacked, str) else unpacked
            ).hexdigest(),
        )
