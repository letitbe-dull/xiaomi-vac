"""Fetch + decrypt + parse a vacuum map into a PNG and the plug-and-play
attribute contract the card consumes. Synchronous; run in an executor."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont
from vacuum_map_parser_base.config.color import ColorsPalette, SupportedColor
from vacuum_map_parser_base.config.drawable import Drawable
from vacuum_map_parser_base.config.image_config import ImageConfig
from vacuum_map_parser_base.config.size import Size, Sizes
from vacuum_map_parser_ijai.map_data_parser import IjaiMapDataParser

from . import map_vector
from .cloud.connector import XiaomiCloud
from .map_parsers import (
    dreame_decrypt_cloud_blob,
    dreame_extract_enckey,
    has_ijai_grid,
    make_parser,
    map_url_endpoint,
    unpack_kwargs,
)

# MIoT property that carries `<object_path>,<enckey>` for cloud-encrypted dreame maps.
# Verified against Tasshack's dreame-vacuum 2026-07-03 (siid=6/piid=3 = OBJECT_NAME).
_DREAME_ENCKEY_SIID = 6
_DREAME_ENCKEY_PIID = 3

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
    Drawable.PATH, Drawable.NO_GO_AREAS, Drawable.VIRTUAL_WALLS,
]
# ROOM_NAMES and CHARGER/VACUUM_POSITION deliberately excluded: the base
# image_generator draws them in a fixed position within the library's own
# draw_map() pass, before this module's own overlays (carpets) ever get a
# chance to run — so they always ended up UNDER anything drawn afterward.
# In particular the vacuum icon should sit ON TOP of a carpet it's parked
# on (as it does physically), not painted over by the carpet's translucent
# fill. We draw all three ourselves (`_draw_carpets` -> `_draw_vacuum_and_
# charger` -> `_draw_room_names`, in that order, called from `fetch()`) so
# the paint order matches reality/legibility. Applies to every brand
# equally (not just
# xiaomi) since it's a MapData-level fix, not a xiaomi-specific one.
_ROOM_NAME_FONT_RATIO = 22  # image width / this = font pixel size
# Bundled instead of relying on Pillow's built-in default font: that font's
# glyph set doesn't cover Hungarian-specific Latin-Extended-A letters (ő/ű,
# U+0151/U+0171) — room names like "Előszoba" rendered with tofu boxes for
# every ő. DejaVu Sans covers the full range we need. (Bitstream Vera /
# DejaVu license, freely redistributable.)
_ROOM_NAME_FONT_PATH = Path(__file__).parent / "fonts" / "DejaVuSans.ttf"

# Shared with the ImageConfig(scale=...) below. `ImageDimensions.to_img()`
# (vacuum_map_parser_base) multiplies every drawn ELEMENT'S POSITION by this,
# but `Sizes` values (icon/path radii and widths) are plain constants the
# generator never scales — bumping just ImageConfig.scale left the charger/
# vacuum-position dots at a fixed 6px, invisibly small on the now-3x-bigger
# image (confirmed 2026-08-01: present in the PNG, but a near-single-pixel
# smudge). Scale `Sizes` by the same factor so icons/paths stay proportional.
_RENDER_SCALE = 3


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
        # scale=3: the raw raster is a small low-res grid (native cell size is
        # tens of mm/px); upscaling here (before any drawing happens) makes
        # the final map noticeably crisper/bigger without touching card-side
        # config — every overlay (rooms/walls/path/vacuum icon, and our own
        # carpets/room-name text below) is computed in the SAME scaled pixel
        # space via `ImageDimensions.to_img()`/our own calibration transform,
        # so nothing needs separate scaling to stay aligned. `Sizes` (icon
        # radii, path width) is a SEPARATE knob the generator never scales
        # by ImageConfig.scale on its own — pass the same factor explicitly
        # (see `_RENDER_SCALE` note above) or the charger/vacuum dots end up
        # a fixed, now-tiny 6px regardless of how big the image got.
        sizes = Sizes({
            Size.VACUUM_RADIUS: 6 * _RENDER_SCALE,
            Size.CHARGER_RADIUS: 6 * _RENDER_SCALE,
            Size.PATH_WIDTH: 1 * _RENDER_SCALE,
            Size.OBSTACLE_RADIUS: 3 * _RENDER_SCALE,
            Size.IGNORED_OBSTACLE_RADIUS: 3 * _RENDER_SCALE,
            Size.OBSTACLE_WITH_PHOTO_RADIUS: 3 * _RENDER_SCALE,
            Size.IGNORED_OBSTACLE_WITH_PHOTO_RADIUS: 3 * _RENDER_SCALE,
            Size.MOP_PATH_WIDTH: 16 * _RENDER_SCALE,
        })
        # Kept (not just handed to make_parser) so `_draw_vacuum_and_charger`
        # can reuse the exact same colors/sizes the excluded CHARGER/
        # VACUUM_POSITION drawables would have used.
        self._palette = ColorsPalette()
        self._sizes = sizes
        self._parser = make_parser(
            self._brand, model, self._palette, sizes, _DRAWABLES,
            ImageConfig(scale=_RENDER_SCALE), [],
        )
        # Inputs for parser.unpack_map; the brand decides which are used.
        self._unpack_kw = unpack_kwargs(
            self._brand, wifi_sn=self._wifi_sn, owner_id=self._user_id,
            device_id=self._device_id, model=self._model, device_mac=self._mac,
        )
        self._endpoint = map_url_endpoint(self._brand)
        self._ijai_grid = has_ijai_grid(self._brand)
        # Dreame cloud enckey polled from siid=6/piid=3 on first fetch; None for
        # unencrypted models or until the property is successfully read.
        self._enckey: str | None = None
        self._enckey_polled = False

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
        All other paths go through parser.unpack_map normally.
        """
        if self._brand == "dreame" and self._enckey is not None:
            from vacuum_map_parser_dreame.map_data_parser import DreameMapDataParser
            if DreameMapDataParser.IVs.get(self._model) is not None:
                return self._parser.unpack_map(raw, enckey=self._enckey)
            return dreame_decrypt_cloud_blob(raw, self._enckey)
        if self._brand == "xiaomi":
            from .xiaomi_json_decrypt import decrypt_xiaomi_json_map
            return decrypt_xiaomi_json_map(raw, self._model, self._device_id)
        return self._parser.unpack_map(raw, **self._unpack_kw)

    @staticmethod
    def _mm_to_pixel_from_calibration(calibration: list) -> "callable | None":
        """Build an (x_mm, y_mm) -> (x_px, y_px) affine mapper from `md.calibration()`.

        `calibration()` gives 3 points relating the SAME raw MIoT position
        coordinate space (the one carpets/rooms/vacuum_position/charger all
        use) to final rendered pixels — it's exactly what the vacuum-position
        dot on third-party map cards is aligned with, so reusing it for
        carpets guarantees the same reference frame, no separate transform
        math needed (two earlier attempts using the xiaomi package's own
        `coord_transformer.map_to_image` and the base library's
        `ImageDimensions.to_img()` both put carpets in visibly wrong spots —
        this sidesteps understanding either one).
        """
        if not calibration or len(calibration) < 3:
            return None
        p0, p1, p2 = calibration[0], calibration[1], calibration[2]
        dvx = p1["vacuum"]["x"] - p0["vacuum"]["x"]
        dvy = p2["vacuum"]["y"] - p0["vacuum"]["y"]
        if not dvx or not dvy:
            return None
        scale_x = (p1["map"]["x"] - p0["map"]["x"]) / dvx
        scale_y = (p2["map"]["y"] - p0["map"]["y"]) / dvy
        off_x = p0["map"]["x"] - p0["vacuum"]["x"] * scale_x
        off_y = p0["map"]["y"] - p0["vacuum"]["y"] * scale_y
        return lambda x, y: (off_x + x * scale_x, off_y + y * scale_y)

    @staticmethod
    def _parse_carpets(unpacked_json: str) -> list[list[float]]:
        """Extract raw carpet rectangles (`[x1,y1,x2,y2,x3,y3,x4,y4]` each,
        native mm unit) from the decrypted xiaomi-JSON map blob's "carpets"
        field. Shared by the PNG overlay (`_draw_carpets`) and the vector
        endpoint (`map_vector.vector_map`'s `carpets=` arg) so both stay in
        sync from one parse.
        """
        try:
            carpets = json.loads(unpacked_json).get("carpets") or []
        except (TypeError, ValueError):
            return []
        out = []
        for c in carpets:
            p = c.get("p") if isinstance(c, dict) else None
            if isinstance(p, list) and len(p) == 8:
                out.append(p)
        return out

    @staticmethod
    def _parse_path(unpacked_json: str) -> list[list[tuple[float, float]]]:
        """Extract the vacuum's traveled trajectory from the xiaomi-JSON map
        blob's "paths" field (`{"pose_id": N, "points": "<json-array-string>"}`
        — note `points` is itself a JSON-encoded STRING, not a nested
        object/list). Neither `vacuum_map_parser_xiaomi` nor `md.path` ever
        populate this for this brand (confirmed 2026-08-04: `md.path` stays
        None) — same gap class as `_parse_carpets`.

        Each point is `{"x","y","type","sweep_mop_mode","yaw"}`, native mm
        unit, same coordinate space as rooms/carpets/vacuum_position. The
        array is a fixed-size preallocated buffer: unwritten slots are
        literal `{"x":0,"y":0,...}` sentinels (confirmed live: exactly the
        first 780 of 1560 slots were the zero-sentinel run, the remaining
        780 were one single contiguous real trajectory) — dropped here by
        keeping only the single longest contiguous run of non-(0,0) points,
        robust even if the buffer ever wraps and the sentinel run isn't at
        the very start.

        Within that run, `type` marks whether a point continues the current
        line (`1`) or starts a brand-new disconnected leg (`0` — e.g. after
        finishing one room and being handed a fresh starting position for
        the next). Confirmed live (2026-08-04, user-reported "physically
        impossible" diagonal cuts through walls on the very first render):
        every large cross-room jump (up to ~4.9m, e.g. Nappali straight to
        Előszoba) sat exactly on a `type 0 -> type 0` pair, while every
        short, real same-room lawnmower-stripe jump (the sparse sampling
        naturally skips the turn arc at the end of each stripe) was
        `type 1 -> type 1`. So: a `type 0` point starts a new segment
        (never connected back to whatever preceded it); consecutive `type 1`
        points extend the current segment. Returns a list of segments (each
        a list of `(x, y)` mm tuples), single-point segments dropped since
        they can't be drawn as a line.
        """
        try:
            raw_points = json.loads(json.loads(unpacked_json).get("paths", {}).get("points") or "[]")
        except (TypeError, ValueError, AttributeError):
            return []
        best: list[dict] = []
        current: list[dict] = []
        for p in raw_points:
            x, y = p.get("x", 0), p.get("y", 0)
            if x == 0 and y == 0:
                if len(current) > len(best):
                    best = current
                current = []
            else:
                current.append(p)
        run = current if len(current) > len(best) else best

        segments: list[list[tuple[float, float]]] = []
        seg: list[tuple[float, float]] = []
        for p in run:
            if p.get("type") == 0 and seg:
                segments.append(seg)
                seg = []
            seg.append((p["x"], p["y"]))
        if seg:
            segments.append(seg)
        return [s for s in segments if len(s) >= 2]

    @staticmethod
    def _parse_xiaomi_grid(unpacked_json: str) -> dict | None:
        """Extract the raw per-cell occupancy grid from the xiaomi-JSON map
        blob for the Atlas card's pixel-accurate room raster (`_roomRaster`
        in xiaomi-vac-card.js) — until now that card only had rectangular
        room BOUNDING BOXES for this brand (`rooms[].bbox`, from `md.rooms`,
        which is all `vacuum_map_parser_xiaomi` extracts), so Atlas drew
        blocky rectangles instead of the true scanned floor shape the
        camera's PNG has always shown (`_LOGGER`-verified 2026-08-01: the
        PNG's own true shape comes from this exact same `map_data` field,
        just consumed differently by `vacuum_map_parser_xiaomi.image_parser`).

        Cell semantics (from the community package's own
        `_normalize_json_map_pixels`/`_room_number_to_grid_id`, reverse
        -engineered rather than reimplemented: their round-trip
        `grid_id -> +7 -> room_number -> -7 -> grid_id` is a no-op, so the
        raw JSON cell value UNDER 3-63 IS the grid_id directly):
          0 = unknown/outside, 1/2 = free floor, 3-63 = room (by grid_id),
          everything else = wall.
        `grid_id` is remapped through the same `map_room_info` (grid_id ->
        room_id) table the parser uses for `md.rooms`' dict keys, so a
        cell's final value matches `rooms[].id` in the vector payload
        exactly — required for the card's tap-to-select highlight
        (`this._sel.has(lab)`) and its room-tint lookup to line up. Falls
        back to the bare grid_id when no explicit mapping entry exists,
        exactly like the community parser's own `grid_to_room.get(x, x)`.

        Row order: verified against `XiaomiImageParser.parse()`'s own y-flip
        (`y = trimmed_height - 1 - img_y`, reading `map_data[img_y*width+x]`
        with no separate trim) — raw row 0 is the SOUTH edge of the map,
        exactly the convention the card's `_roomRaster` already assumes for
        the ijai grid (`(H-1-row)*W+col`), so no re-flip is needed here.
        Returns None (never raises) on any structural surprise — a missing
        grid just means Atlas keeps showing bbox rectangles, same as today.
        """
        try:
            payload = json.loads(unpacked_json)
            w, h = int(payload["width"]), int(payload["height"])
            res_mm = float(payload.get("resolution", 50))
            origin_x_mm = float(payload.get("origin_x", 0))
            origin_y_mm = float(payload.get("origin_y", 0))
            raw = zlib.decompress(base64.b64decode(payload["map_data"]))
        except Exception as ex:  # noqa: BLE001
            _LOGGER.debug("xiaomi grid: could not read map_data: %s", ex)
            return None
        if len(raw) < w * h or w <= 0 or h <= 0:
            return None

        grid_to_room: dict[int, int] = {}
        room_info = payload.get("map_room_info")
        if isinstance(room_info, list):
            for entry in room_info:
                if not isinstance(entry, dict):
                    continue
                try:
                    grid_to_room[int(entry["grid_id"])] = int(entry["room_id"])
                except (KeyError, TypeError, ValueError):
                    continue

        normalized = bytearray(w * h)
        for i in range(w * h):
            v = raw[i]
            if v == 0:
                normalized[i] = 0
            elif v in (1, 2):
                normalized[i] = 127
            elif 3 <= v <= 63:
                normalized[i] = grid_to_room.get(v, v) & 0xFF
            else:
                normalized[i] = 128

        return {
            "size": {"x": w, "y": h},
            "bounds": {
                "minX": origin_x_mm * 0.001, "minY": origin_y_mm * 0.001,
                "maxX": (origin_x_mm + w * res_mm) * 0.001,
                "maxY": (origin_y_mm + h * res_mm) * 0.001,
            },
            "resolution": res_mm * 0.001,
            "grid_rle": map_vector._rle(bytes(normalized)),
            # Real room ids for this brand can be small (this house: 3-6),
            # well outside ijai's native 10-59/60-109 window — the card
            # reads these bounds from `m.legend` instead of assuming ijai's
            # numbers, so ijai's own rendering is untouched by this.
            "legend": {
                "outside": 0, "floor": 127, "new_area": 127, "wall": 128,
                "room_min": 1, "room_max": 126,
                "selected_room_min": 1000, "selected_room_max": 999,  # unused/unreachable for this brand
            },
        }

    def _draw_carpets(
        self, img: Image.Image, carpets: list[list[float]], transform, off_x: int, off_y: int,
    ) -> Image.Image:
        """Overlay detected-carpet rectangles onto the already-cropped map image.

        `vacuum_map_parser_xiaomi` never reads the "carpets" field the cloud
        blob actually contains (confirmed 2026-08-01 against a live blob for
        this model: 8 rectangles, each `{"p": [x1,y1,...,x4,y4]}`, same raw
        MIoT position coordinate space as rooms/vacuum_position/charger).
        Draw them ourselves — no vendored-package changes needed.

        Runs on the POST-`_autocrop` image (`off_x`/`off_y` = the same crop
        offset `calibration_points` get shifted by): drawing before crop let
        real, correctly-positioned-but-large carpet rectangles expand
        `_autocrop`'s uniform-background bounding box, shrinking everything
        else in the final image and clipping room-name text — not a
        coordinate bug, just wrong ordering. Best-effort: any failure here
        just means no carpet overlay, never breaks the map.

        Uses a translucent fill (not outline-only): safe now that room-name
        text is drawn separately, AFTER this (`_draw_room_names`, called
        later in `fetch()`) — the original outline-only choice was a
        workaround for text getting obscured when both were baked in the
        same pass by the upstream library; now that we own the draw order,
        the nicer filled look is legible again.
        """
        if not carpets:
            return img
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        draw = ImageDraw.Draw(img, "RGBA")
        for p in carpets:
            try:
                pixels = [transform(p[i], p[i + 1]) for i in range(0, 8, 2)]
                pixels = [(x - off_x, y - off_y) for x, y in pixels]
                draw.polygon(pixels, fill=(210, 170, 90, 70), outline=(180, 138, 60, 225), width=2)
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Could not draw carpet region %s: %s", p, ex)
        return img

    def _draw_path(
        self, img: Image.Image, segments: list[list[tuple[float, float]]], transform, off_x: int, off_y: int,
    ) -> Image.Image:
        """Overlay the vacuum's traveled trajectory (see `_parse_path`) onto
        the already-cropped map image. Drawn AFTER carpets (visible on top
        of them, like the real dirt trail would be) but BEFORE the
        vacuum/charger icons (so the vacuum icon sits on top of its own
        trail's current endpoint) and BEFORE room-name text (kept on top,
        per the established z-order in `fetch()`).

        `segments` is a list of independent point-runs (see `_parse_path`'s
        `type`-based split) — each drawn as its OWN `draw.line()` call, never
        connected to the previous segment's last point. Drawing them as one
        big line would paint a straight "impossible" cut wherever the
        vacuum's own data marks a new leg (confirmed live: cross-room jumps
        up to ~4.9m through walls, before this per-segment split existed).
        Best-effort: any single segment failing just skips that segment,
        never breaks the whole map.
        """
        if not segments:
            return img
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        draw = ImageDraw.Draw(img, "RGBA")
        for seg in segments:
            if len(seg) < 2:
                continue
            try:
                pixels = [transform(x, y) for x, y in seg]
                pixels = [(x - off_x, y - off_y) for x, y in pixels]
                draw.line(pixels, fill=(64, 200, 255, 210), width=2 * _RENDER_SCALE, joint="curve")
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Could not draw vacuum path segment: %s", ex)
        return img

    def _draw_vacuum_and_charger(
        self, img: Image.Image, md, transform, off_x: int, off_y: int,
    ) -> Image.Image:
        """Draw the charger + vacuum-position icons ourselves, AFTER carpets.

        Ported from `vacuum_map_parser_base.image_generator.ImageGenerator.
        _draw_vacuum`/`_draw_pieslice` (same visual style, same
        ColorsPalette/Sizes config) — the library version runs inside its
        own draw_map() pass, before carpets exist, so a carpet the vacuum is
        parked on would paint right over it. In reality the vacuum sits ON
        TOP of a carpet, not under it — draw order now matches that.
        Best-effort: any failure here just means no icon, never breaks the map.
        """
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        draw = ImageDraw.Draw(img, "RGBA")
        r_vac = self._sizes.get_size(Size.VACUUM_RADIUS)
        r_chg = self._sizes.get_size(Size.CHARGER_RADIUS)
        robo = self._palette.get_color(SupportedColor.ROBO)
        robo_outline = self._palette.get_color(SupportedColor.ROBO_OUTLINE)
        charger_fill = self._palette.get_color(SupportedColor.CHARGER)
        charger_outline = self._palette.get_color(SupportedColor.CHARGER_OUTLINE)
        if md.charger is not None:
            try:
                x, y = transform(md.charger.x, md.charger.y)
                x, y = x - off_x, y - off_y
                angle = -md.charger.a if md.charger.a is not None else 0
                coords = ((x - r_chg, y - r_chg), (x + r_chg, y + r_chg))
                draw.pieslice(coords, angle + 90, angle - 90, outline=charger_outline, fill=charger_fill)
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Could not draw charger icon: %s", ex)
        if md.vacuum_position is not None:
            try:
                x, y = transform(md.vacuum_position.x, md.vacuum_position.y)
                x, y = x - off_x, y - off_y
                a = md.vacuum_position.a if md.vacuum_position.a is not None else 0
                r_scaled = r_vac / 16
                draw.ellipse([x - r_vac, y - r_vac, x + r_vac, y + r_vac], outline=robo_outline, fill=robo)
                if r_vac >= 8:
                    r2 = r_scaled * 14
                    draw.ellipse([x - r2, y - r2, x + r2, y + r2], outline=robo_outline)
                a1 = (a + 104) / 180 * math.pi
                a2 = (a - 104) / 180 * math.pi
                r2 = r_scaled * 13
                x1, y1 = x - r2 * math.cos(a1), y + r2 * math.sin(a1)
                x2, y2 = x - r2 * math.cos(a2), y + r2 * math.sin(a2)
                draw.line([x1, y1, x2, y2], width=1, fill=robo_outline)
                angle_rad = a / 180 * math.pi
                r2 = r_scaled * 3
                lx, ly = x + r2 * math.cos(angle_rad), y - r2 * math.sin(angle_rad)
                r2 = r_scaled * 4
                draw.ellipse([lx - r2, ly - r2, lx + r2, ly + r2], outline=robo_outline, fill=robo)
                half = tuple((robo_outline[i] + robo[i]) // 2 for i in range(3))
                r2 = r_scaled * 10
                bx, by = x + r2 * math.cos(angle_rad), y - r2 * math.sin(angle_rad)
                r2 = r_scaled * 2
                draw.ellipse([bx - r2, by - r2, bx + r2, by + r2], outline=half, fill=half)
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Could not draw vacuum icon: %s", ex)
        return img

    def _draw_room_names(
        self, img: Image.Image, rooms: dict, transform, off_x: int, off_y: int,
    ) -> Image.Image:
        """Draw room-name labels ourselves, LAST, so text always paints on
        top of every other overlay (carpets, no-go zones, walls, path).

        The upstream `image_generator._draw_room_names` runs inside the
        library's own `draw_map()` pass (see `_DRAWABLES` above for why it's
        excluded there) using a fixed, non-scaling PIL bitmap font — we
        reimplement it here with a size proportional to the (now upscaled,
        see `ImageConfig(scale=3)`) image, plus a white stroke/halo so the
        text stays legible over any floor color or overlay it happens to sit
        on. Best-effort: any single-room draw failure is skipped, never
        breaks the whole map.
        """
        if not rooms:
            return img
        if img.mode != "RGBA":
            img = img.convert("RGBA")
        draw = ImageDraw.Draw(img, "RGBA")
        font_size = max(11, round(img.width / _ROOM_NAME_FONT_RATIO))
        try:
            font = ImageFont.truetype(str(_ROOM_NAME_FONT_PATH), font_size)
        except OSError:
            _LOGGER.debug("Bundled room-name font missing/unreadable, falling back")
            try:
                font = ImageFont.load_default(size=font_size)
            except TypeError:
                # Pillow < 10.1: load_default() takes no `size` arg at all.
                font = ImageFont.load_default()
        for room in rooms.values():
            p = room.point()
            if p is None:
                continue
            try:
                x, y = transform(p.x, p.y)
                x -= off_x
                y -= off_y
                l, t, r, b = draw.textbbox((0, 0), room.name, font=font)
                w, h = r - l, b - t
                draw.text(
                    (x - w / 2, y - h / 2), room.name, font=font,
                    fill=(25, 25, 25, 255), stroke_width=2, stroke_fill=(255, 255, 255, 220),
                )
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Could not draw room name %r: %s", getattr(room, "name", None), ex)
        return img

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
        url = self._cloud.map_url(self._server, self._device_id, slot, self._endpoint)
        if not url:
            # No URL usually means the cloud session expired; let the
            # coordinator try a token refresh.
            raise SessionExpired()
        raw = self._cloud.download(url)
        if not raw:
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
            _LOGGER.debug("Could not decrypt map at slot %s: %s", slot, ex)
            return None
        carpets = self._parse_carpets(unpacked) if self._brand == "xiaomi" else []
        xiaomi_grid = self._parse_xiaomi_grid(unpacked) if self._brand == "xiaomi" else None
        path_points = self._parse_path(unpacked) if self._brand == "xiaomi" else []
        try:
            md = self._parser.parse(unpacked)
            vector_scale = 0.001 if self._brand == "xiaomi" else 1.0
            vector = map_vector.vector_map(
                md, unpacked, ijai_grid=self._ijai_grid, scale=vector_scale, carpets=carpets,
                xiaomi_grid=xiaomi_grid, path=path_points,
            )
        except Exception as ex:  # noqa: BLE001
            # Decrypted fine but the parser rejected the frame (corrupt or
            # unexpected layout). The key material is good — keep the enckey.
            _LOGGER.debug("Parser rejected map frame at slot %s: %s", slot, ex)
            return None
        if md.image is None or md.image.is_empty:
            _LOGGER.debug("Parsed map at slot %s is empty", slot)
            return None

        cropped, off_x, off_y = _autocrop(md.image.data)

        # Shared mm->px transform for every overlay we draw ourselves, AFTER
        # the library's own draw_map() pass and AFTER autocrop — carpets
        # first (so the vacuum sits visibly on top of one it's parked on,
        # matching reality), then the traveled path (on top of carpets),
        # then vacuum/charger (on top of the path's own endpoint), room-name
        # text last so text always ends up on top of everything else.
        transform = self._mm_to_pixel_from_calibration(md.calibration())
        if transform is not None:
            if self._brand == "xiaomi" and carpets:
                cropped = self._draw_carpets(cropped, carpets, transform, off_x, off_y)
            if self._brand == "xiaomi" and path_points:
                cropped = self._draw_path(cropped, path_points, transform, off_x, off_y)
            cropped = self._draw_vacuum_and_charger(cropped, md, transform, off_x, off_y)
            cropped = self._draw_room_names(cropped, md.rooms or {}, transform, off_x, off_y)

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
        }
        return MapResult(
            image_png=buf.getvalue(),
            attributes=attributes,
            vector=vector,
            map_id=vector.get("map_id"),
            content_hash=hashlib.sha256(
                unpacked.encode("utf-8") if isinstance(unpacked, str) else unpacked
            ).hexdigest(),
        )
