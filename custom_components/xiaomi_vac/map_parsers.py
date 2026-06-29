"""Per-brand map parser dispatch.

`map.py` was hard-wired to ijai. Each launch brand ships its own parser in the
Piotr Machowski family (all on the same `vacuum-map-parser-base`). They share the
`parse()` output — the base `MapData` (image/rooms/charger/vacuum/path/walls/
zones) — so the PNG + attribute contract in `map.py` is already brand-agnostic.
They differ in three places, handled here:

  - the constructor (dreame also takes `model`, to pick its per-model AES IV);
  - `unpack_map()` inputs — the cloud blob's key derivation differs per brand
    (verified against each parser's source):
      ijai   : wifi_sn + owner_id + device_id + model + device_mac  (AES-ECB)
      xiaomi : model + device_id                                    (AES-CBC)
      dreame : enckey (optional) + a built-in per-model IV; no enckey -> the
               parser falls back to plain zlib (works for UNENCRYPTED dreame
               maps only)
      viomi  : none — the blob is plain zlib;
  - whether the unpacked blob is an ijai `RobotMap` protobuf. Only ijai is, and
    that's what `map_vector.extract_grid()` needs for the crisp labelled grid.
    Other brands have no equivalent raw grid, so they ship vector overlays
    (in metres) + the rendered PNG, and `ijai_grid=False` (best-effort, see TODO
    8 — core control never depends on the map).

Parsers are imported lazily so a brand whose dep isn't installed degrades to an
"unsupported map" error at fetch time rather than breaking module import.
"""
from __future__ import annotations

from typing import Any

# Launch brands with a wired map parser. roidmi's parser exists on PyPI but is
# post-launch (its profile has no core, so the device never builds anyway).
SUPPORTED_BRANDS = ("ijai", "xiaomi", "dreame", "viomi")


def brand_of(model: str) -> str:
    """First dotted segment of a model id, e.g. ``ijai.vacuum.v17`` -> ``ijai``."""
    return model.split(".")[0]


def has_ijai_grid(brand: str) -> bool:
    """True only for ijai: its unpacked blob is the `RobotMap` protobuf the
    crisp labelled-grid extractor (`map_vector.extract_grid`) reads."""
    return brand == "ijai"


def make_parser(brand: str, model: str, palette, sizes, drawables, image_config, texts):
    """Construct the parser for ``brand`` (lazy import). Raises ValueError for an
    unknown brand, ImportError if the brand's dep isn't installed."""
    if brand == "ijai":
        from vacuum_map_parser_ijai.map_data_parser import IjaiMapDataParser
        return IjaiMapDataParser(palette, sizes, drawables, image_config, texts)
    if brand == "xiaomi":
        from vacuum_map_parser_xiaomi.map_data_parser import XiaomiMapDataParser
        return XiaomiMapDataParser(palette, sizes, drawables, image_config, texts)
    if brand == "dreame":
        from vacuum_map_parser_dreame.map_data_parser import DreameMapDataParser
        # dreame needs the model to select its per-model decryption IV.
        return DreameMapDataParser(palette, sizes, drawables, image_config, texts, model)
    if brand == "viomi":
        from vacuum_map_parser_viomi.map_data_parser import ViomiMapDataParser
        return ViomiMapDataParser(palette, sizes, drawables, image_config, texts)
    raise ValueError(f"no map parser for brand {brand!r}")


def required_map_key_inputs(brand: str) -> frozenset[str]:
    """Keys that MUST be non-empty before a MapFetcher can be constructed.

    ijai   : wifi_sn + device_mac (both feed the AES-ECB key derivation)
    xiaomi : model + device_id   (AES-CBC IV derivation; both always present)
    dreame : none required        (best-effort; encrypted maps have no verified
                                   enckey source, so the parser falls back to
                                   plain zlib for unencrypted models)
    viomi  : none                 (plain zlib; no local key material needed)
    """
    if brand == "ijai":
        return frozenset({"wifi_sn", "device_mac"})
    if brand in ("xiaomi", "dreame", "viomi"):
        return frozenset()
    raise ValueError(f"no map parser for brand {brand!r}")


def unpack_kwargs(
    brand: str,
    *,
    wifi_sn: str,
    owner_id: str,
    device_id: str,
    model: str,
    device_mac: str,
    enckey: str | None = None,
) -> dict[str, Any]:
    """Keyword args for ``parser.unpack_map`` for this brand (see module docstring
    for the per-brand key derivation each set feeds)."""
    if brand == "ijai":
        return {
            "wifi_sn": wifi_sn,
            "owner_id": owner_id,
            "device_id": device_id,
            "model": model,
            "device_mac": device_mac,
        }
    if brand == "xiaomi":
        return {"model": model, "device_id": device_id}
    if brand == "dreame":
        # enckey is None for unencrypted dreame maps (most launch models) and for
        # the IV-protected models until a verified enckey source is wired (TODO 8).
        return {"enckey": enckey} if enckey is not None else {}
    if brand == "viomi":
        return {}
    raise ValueError(f"no map parser for brand {brand!r}")
