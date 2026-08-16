"""Extract a vector/grid map representation from the decoded ijai blob.

See docs/dev/map-pipeline.md for the per-brand map pipeline details.
"""
from __future__ import annotations

from typing import Any

import vacuum_map_parser_ijai.RobotMap_pb2 as RobotMap


def _rle(grid: bytes) -> list[int]:
    """Run-length encode the grid as a flat [value, count, value, count, ...]."""
    out: list[int] = []
    if not grid:
        return out
    prev = grid[0]
    run = 1
    for b in grid[1:]:
        if b == prev and run < 0xFFFFFFFF:
            run += 1
        else:
            out.append(prev)
            out.append(run)
            prev = b
            run = 1
    out.append(prev)
    out.append(run)
    return out


# --- room-contour tracing -------------------------------------------------
# The firmware's roomChain is a coarse ~8-vertex cartoon of each room. The
# labelled occupancy grid is ground truth: one byte/cell, room id 10-59 (or the
# same id + 50 when that room is "selected"). We trace the EXACT cell outline of
# each room along grid lines — no smoothing — so the card renders pixel-true
# room areas (the staircase is sub-pixel at card size, exactly like the raw blob
# the official app draws). Each room becomes one {id, rings:[[[col,row],...]]}
# entry; multiple rings (disconnected pieces + furniture holes) render as a
# single even-odd path, so holes punch through. id == grid label == md.rooms key.

ROOM_MIN, ROOM_MAX = 10, 59
SELECTED_OFFSET = 50  # selected-room cell value = base id + 50 (60-109)


def _label_of(v: int) -> int | None:
    """Map a cell value to its base room id, or None if it isn't a room."""
    if ROOM_MIN <= v <= ROOM_MAX:
        return v
    if ROOM_MAX + SELECTED_OFFSET >= v >= ROOM_MIN + SELECTED_OFFSET:
        return v - SELECTED_OFFSET
    return None


def _trace_mask(mask: set) -> list[list[list[int]]]:
    """Trace exact boundary loops of a set of (col,row) cells along grid lines.

    Each empty-neighbour side of a filled cell is a directed unit edge wound so
    the interior stays on the right. Edges stitch end-to-start into closed loops.

    At a "pinch" vertex — where the boundary touches itself at a single corner
    (two cells meeting only diagonally, which is everywhere in noisy scan data) —
    a corner is the start of TWO edges. A plain start->end map loses one, the
    walk dead-ends, and the loop closes with a stray diagonal chord. So we keep a
    multimap and, at every vertex, leave by the most-clockwise turn relative to
    how we arrived. That consistently separates the touching loops instead of
    tangling them.
    """
    adj: dict[tuple, list[tuple]] = {}
    remaining: set[tuple] = set()  # (start, end) edges not yet walked
    for (c, r) in mask:
        for a, b in (
            ((c, r), (c + 1, r)) if (c, r - 1) not in mask else (None, None),
            ((c + 1, r), (c + 1, r + 1)) if (c + 1, r) not in mask else (None, None),
            ((c + 1, r + 1), (c, r + 1)) if (c, r + 1) not in mask else (None, None),
            ((c, r + 1), (c, r)) if (c - 1, r) not in mask else (None, None),
        ):
            if a is not None:
                adj.setdefault(a, []).append(b)
                remaining.add((a, b))

    def _next(cur: tuple, din: tuple, cands: list[tuple]) -> tuple:
        # pick the outgoing edge that turns most clockwise from incoming dir din.
        # rank: right turn (0) < straight (1) < left (2) < u-turn (3); interior is
        # on the right, so hugging clockwise keeps each loop separate at a pinch.
        def rank(en: tuple) -> int:
            dx, dy = en[0] - cur[0], en[1] - cur[1]
            cross = din[0] * dy - din[1] * dx   # >0 = left turn (y-down grid)
            dot = din[0] * dx + din[1] * dy
            if cross < 0:
                return 0                        # right turn
            if cross == 0:
                return 1 if dot > 0 else 3      # straight vs u-turn
            return 2                            # left turn
        return min(cands, key=rank)

    loops = []
    while remaining:
        start, end = next(iter(remaining))
        remaining.discard((start, end))
        loop = [list(start), list(end)]
        prev, cur = start, end
        while cur != start:
            din = (cur[0] - prev[0], cur[1] - prev[1])
            cands = [e for e in adj.get(cur, ()) if (cur, e) in remaining]
            if not cands:
                break
            nxt = _next(cur, din, cands)
            remaining.discard((cur, nxt))
            prev, cur = cur, nxt
            loop.append(list(cur))
        if cur == start:
            loop.pop()                          # drop the repeated start vertex
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _drop_collinear(loop: list[list[int]]) -> list[list[int]]:
    """Drop vertices mid-run along a straight axis edge — keeps the staircase
    shape pixel-identical but shrinks long flat walls to their two endpoints."""
    n = len(loop)
    if n < 3:
        return loop
    out = []
    for i in range(n):
        a, b, cc = loop[(i - 1) % n], loop[i], loop[(i + 1) % n]
        # cross product (b-a)x(c-b); 0 => b lies on the a->c line, so drop it
        if (b[0] - a[0]) * (cc[1] - b[1]) - (b[1] - a[1]) * (cc[0] - b[0]) != 0:
            out.append(b)
    return out or loop


def _signed_area(loop: list[list[int]]) -> float:
    """Twice the signed polygon area (shoelace); sign encodes winding."""
    s = 0.0
    n = len(loop)
    for i in range(n):
        x0, y0 = loop[i]
        x1, y1 = loop[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return s


def trace_room_chains(grid: bytes, w: int, h: int) -> list[dict[str, Any]]:
    """Trace exact room outlines from the labelled grid (row-major, w*h cells).

    Follows what proven renderers (Valetudo) do: each room is a SOLID area,
    obstacles are a separate layer drawn on top — never holes punched into the
    fill. So we keep only the OUTER boundary of each room component and drop
    interior holes (furniture/wall cells). Outline is exact (no smoothing); the
    card fills it with no stroke, so the staircase is sub-pixel like the raw blob.
    """
    masks: dict[int, set] = {}
    for r in range(h):
        base = r * w
        for c in range(w):
            lab = _label_of(grid[base + c])
            if lab is not None:
                masks.setdefault(lab, set()).add((c, r))
    chains: list[dict[str, Any]] = []
    for lab in sorted(masks):
        loops = _trace_mask(masks[lab])
        if not loops:
            continue
        # Outer boundaries share the winding of the largest loop; holes wind the
        # other way. Keep outer rings only (one per disconnected room piece).
        outer = _signed_area(max(loops, key=lambda L: abs(_signed_area(L)))) >= 0
        rings = [
            _drop_collinear(loop)
            for loop in loops
            if (_signed_area(loop) >= 0) == outer and len(loop) >= 3
        ]
        if rings:
            chains.append({"id": lab, "rings": rings})
    return chains


def extract_grid(unpacked: bytes) -> dict[str, Any]:
    """Pull the labelled grid + transform + room chains from the unpacked blob.

    Vector overlays already in metres (path/charger/walls/...) come from the
    parser's MapData; this returns only what the parser discards: the raw grid,
    the cell<->metre bounds, and the room boundary chains.
    """
    rm = RobotMap.RobotMap()
    rm.ParseFromString(unpacked)
    h = rm.mapHead
    grid = bytes(rm.mapData.mapData)

    # Prefer contours traced from the labelled grid (true wall-aligned edges).
    # Fall back to the firmware's coarse roomChain only if the grid has no
    # labelled rooms (e.g. a fresh/quick map with vector chains but no fill).
    chains = trace_room_chains(grid, h.sizeX, h.sizeY)
    if not chains:
        for room in rm.roomChain:
            pts = [[p.x, p.y] for p in room.points]
            if pts:
                chains.append({"id": room.roomId, "rings": [pts]})

    return {
        "map_id": h.mapHeadId,  # identifies the physical map (dedupe across slots)
        "size": {"x": h.sizeX, "y": h.sizeY},
        # cell (col,row) centre in metres:
        #   mx = minX + (col + 0.5) * (maxX - minX) / sizeX
        #   my = minY + (row + 0.5) * (maxY - minY) / sizeY
        # and the inverse for metre -> cell. Overlays use the SAME transform so
        # they line up with the grid regardless of render orientation.
        "bounds": {"minX": h.minX, "minY": h.minY, "maxX": h.maxX, "maxY": h.maxY},
        "resolution": h.resolution,
        "grid_rle": _rle(grid),  # row-major, len == sizeX*sizeY when expanded
        "room_chains": chains,   # grid-cell polygons; may be empty
        # legend so the card can theme cell types without magic numbers
        "legend": {
            "outside": 0, "floor": 1, "new_area": 2, "wall": 255,
            "room_min": 10, "room_max": 59,
            "selected_room_min": 60, "selected_room_max": 109,
        },
    }


def _empty_grid() -> dict[str, Any]:
    """Grid-less contract for brands whose unpacked blob is NOT an ijai protobuf.

    The labelled occupancy grid + room contours are ijai-only (`extract_grid`).
    Other brands (xiaomi/dreame/viomi) ship the rendered PNG + vector overlays
    (already in metres) instead; the card overlays those using the attribute
    `calibration_points` rather than this grid. Same key shape, just empty.
    """
    return {
        "map_id": None,
        "size": None,
        "bounds": None,
        "resolution": None,
        "grid_rle": [],
        "room_chains": [],
        "legend": {
            "outside": 0, "floor": 1, "new_area": 2, "wall": 255,
            "room_min": 10, "room_max": 59,
            "selected_room_min": 60, "selected_room_max": 109,
        },
    }


def vector_map(
    md: Any, unpacked: bytes, *, ijai_grid: bool = True, scale: float = 1.0,
    carpets: list | None = None, xiaomi_grid: dict | None = None,
    path: list | None = None,
) -> dict[str, Any]:
    """Assemble the card contract: grid (this module) + vector overlays (md).

    `md` is the parsed vacuum_map_parser_base.MapData. All overlay coords are
    expected in METRES by the card's SVG (font sizes, icon radii, padding are
    all metre-scale constants). `ijai_grid` is True only when `unpacked` is an
    ijai `RobotMap` protobuf (the sole source of the crisp labelled grid);
    other brands pass False and get the overlays-only contract.

    `scale` converts `md`'s NATIVE coordinate unit to metres — 1.0 for
    brands whose MapData is already metre-scale (ijai; presumably
    dreame/viomi too). The xiaomi-JSON brand's raw MIoT position unit is
    millimetres, not metres (confirmed 2026-08-01: room bbox extents came out
    ~10700x5900 unscaled — a 10km-wide "room" — which silently made every
    text label and the vacuum-position dot microscopic relative to the SVG
    viewBox, not actually missing). Callers pass `scale=0.001` for that case.

    `carpets` (optional): raw detected-carpet rectangles as 8-number flat
    lists `[x1,y1,x2,y2,x3,y3,x4,y4]`, same native unit as everything else
    in `md` — the xiaomi-JSON blob's own "carpets" field, which neither
    `vacuum_map_parser_xiaomi` nor this module's MapData source parses on
    its own (see the 2026-08-01 map.py `_draw_carpets` note). Caller is
    responsible for extracting them from the raw payload; this just scales
    and passes them through.

    `xiaomi_grid` (optional): the pre-built {size, bounds, resolution,
    grid_rle, legend} contract from `map.py`'s `MapFetcher._parse_xiaomi_grid`
    — already-decoded raw occupancy grid for xiaomi-JSON devices (already in
    METRES, no further `scale` multiplication needed here since that
    conversion happened at extraction time). Ignored when `ijai_grid` is
    True (ijai's own `extract_grid` is authoritative there).

    `path` (optional): the xiaomi-JSON blob's own "paths" trajectory, as a
    list of independent segments (each a list of `(x, y)` mm tuples),
    already filtered down to the real contiguous run AND split on the
    "new leg" `type` marker by `map.py`'s `MapFetcher._parse_path` (that
    field's array is a fixed-size buffer with unwritten `{0,0}` sentinel
    slots, and drawing every real point as ONE continuous line produces
    physically-impossible straight cuts through walls between legs —
    confirmed live 2026-08-04, see `_parse_path`'s docstring). Takes
    priority over `md.path` when both are given; in practice they're
    mutually exclusive (only xiaomi ever passes `path`, only ijai's own
    MapData ever populates `md.path`).

    Output contract for `out["path"]`: always a list of segments (never a
    flat point list), i.e. `[[[x,y],[x,y],...], [[x,y],...], ...]` — one
    inner list per line the card should draw independently. `md.path.path`
    (ijai) is ALSO already shaped as sub-paths internally, so this unifies
    both brands under one contract instead of the previous flat-list shape
    (which silently joined ijai's own sub-paths together the same
    physically-wrong way — a latent, previously-unnoticed instance of the
    exact same bug this was written to fix for xiaomi).
    """
    if ijai_grid:
        out = extract_grid(unpacked)
    elif xiaomi_grid:
        out = {**_empty_grid(), **xiaomi_grid}
    else:
        out = _empty_grid()

    if carpets:
        out["carpets"] = [[v * scale for v in c] for c in carpets if len(c) == 8]

    if path:
        out["path"] = [[[x * scale, y * scale] for x, y in seg] for seg in path if len(seg) >= 2]
    elif md.path is not None:
        out["path"] = [
            [[p.x * scale, p.y * scale] for p in sub] for sub in md.path.path if len(sub) >= 2
        ]
    if md.charger is not None:
        out["charger"] = {"x": md.charger.x * scale, "y": md.charger.y * scale}
    if md.vacuum_position is not None:
        out["vacuum"] = {"x": md.vacuum_position.x * scale, "y": md.vacuum_position.y * scale}
    if md.goto is not None:
        out["goto"] = {"x": md.goto.x * scale, "y": md.goto.y * scale}

    out["rooms"] = [
        {
            "id": rid,
            "name": r.name,
            "cx": r.pos_x * scale,
            "cy": r.pos_y * scale,
            "bbox": [r.x0 * scale, r.y0 * scale, r.x1 * scale, r.y1 * scale],
        }
        for rid, r in (md.rooms or {}).items()
    ]
    out["walls"] = [[w.x0 * scale, w.y0 * scale, w.x1 * scale, w.y1 * scale] for w in (md.walls or [])]
    out["no_go"] = [[v * scale for v in a.as_list()] for a in (md.no_go_areas or [])]
    out["no_mop"] = [[v * scale for v in a.as_list()] for a in (md.no_mopping_areas or [])]
    out["zones"] = [[z.x0 * scale, z.y0 * scale, z.x1 * scale, z.y1 * scale] for z in (md.zones or [])]
    out["vacuum_room"] = md.vacuum_room
    out["vacuum_room_name"] = md.vacuum_room_name
    return out
