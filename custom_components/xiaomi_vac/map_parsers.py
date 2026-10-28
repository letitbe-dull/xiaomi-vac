"""Per-brand map parser dispatch (construction, key derivation, unpack kwargs).

See docs/dev/map-pipeline.md for the per-brand map pipeline details.
"""
from __future__ import annotations

import base64
import hashlib
import zlib
from typing import Any

SUPPORTED_BRANDS = ("ijai", "xiaomi", "dreame", "viomi", "roidmi")


def dreame_extract_enckey(prop_value: str) -> str | None:
    """Extract the enckey from MIoT property siid=6/piid=3 value `<object_path>,<enckey>`.

    Returns None when the value contains no comma (unencrypted dreame model).
    Verified against Tasshack's dreame-vacuum 2026-07-03.
    """
    if not prop_value or "," not in prop_value:
        return None
    parts = prop_value.split(",")
    # `or None`: an empty key ("path,") means no usable enckey.
    return (parts[1] or None) if len(parts) > 1 else None


def dreame_extract_object_name(prop_value: str) -> str | None:
    """Extract the cloud object path from MIoT property siid=6/piid=3 value `<object_path>,<enckey>`.

    Returns None when the value has no comma or an empty path.
    """
    if not prop_value or "," not in prop_value:
        return None
    return prop_value.split(",")[0] or None


def dreame_decrypt_cloud_blob(raw: bytes, enckey: str) -> bytes:
    """Decrypt and decompress a dreame cloud map blob.

    Implements the Tasshack-verified decode chain (dreame/map.py ~L1883-1907):
      1. URL-safe base64 normalise (replace _ and -)
      2. base64 decode
      3. AES-256-CBC: key = SHA256(enckey)[:32], IV = 16 zero bytes
      4. zlib decompress

    Returns decompressed map bytes ready for parser.parse(). Call directly
    instead of parser.unpack_map() for models not in the parser's built-in
    IVs dict (those use a zero IV, not a model-specific IV).
    """
    try:
        from Crypto.Cipher import AES
    except ModuleNotFoundError:  # pragma: no cover
        from Cryptodome.Cipher import AES

    raw_str = raw.decode().replace("_", "/").replace("-", "+")
    raw_bytes = base64.decodebytes(raw_str.encode("utf8"))
    key = hashlib.sha256(enckey.encode()).hexdigest()[:32].encode("utf8")
    decrypted = AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16).decrypt(raw_bytes)
    return zlib.decompress(decrypted)


# New-generation xiaomi profiles whose cloud map is the JSON format that
# vacuum-map-parser-xiaomi handles. Confirmed models per the upstream README
# (github.com/PiotrMachowski/Python-package-vacuum-map-parser-xiaomi, verified
# 2026-07-03): e101gb, ov31gl, ov71gl, ov81gl. ov21gl is absent from both
# parser READMEs; assumed xiaomi-JSON by family resemblance — route to ijai
# only if hardware testing contradicts this. Every other xiaomi.* profile is
# an ijai-engine rebrand (RobotMap protobuf: c103/c104/b106eu etc.).
#
# ov42gl/ov43gb (2026-07-31): same reasoning as ov21gl — same siid=2 core
# layout AND (ov42gl only, hardware-confirmed) the same siid=10 vacuum-map
# service (Map Obj Name/Trajectory Obj Name/Map Management/Room Information),
# so they belong to the same map family. Routing them through the *ijai*
# parser/endpoint instead (the pre-fix behaviour: any xiaomi.* profile not in
# this set falls back to "ijai") is the leading suspect for the live
# "-6 invalid config for fds" cloud error on ov42gl: it picks the wrong
# `get_interim_file_url_pro` endpoint (ijai-only, see map_url_endpoint())
# instead of the plain `get_interim_file_url` this family actually needs, and
# would hand a JSON blob to the ijai protobuf parser even if the URL fetch
# succeeded. Needs a live camera-entity retest to confirm; ov43gb is added on
# spec-family grounds only (no real device to test against yet).
#
# c107/d101/d102ev/d102gl/d109gl (2026-08-13): same siid=2 core layout as
# ov21gl, so the same map family. d109gl is hardware-confirmed: before this
# fix the ijai fallback produced "Could not decrypt map at slot 0: Data must
# be aligned to block boundary in ECB mode" every cycle (map camera stuck
# unavailable); routing to the xiaomi JSON parser fixes it (decrypt key comes
# from model[-16:], see xiaomi_json_decrypt.py) and the live map + card work.
# c107/d101/d102ev/d102gl share the identical replace(XIAOMI_C107, ...)
# profile layout and are added on spec-family grounds only (no hardware yet).
_XIAOMI_JSON_MAP_PROFILES = frozenset({
    "xiaomi.e101gb",
    "xiaomi.d109gl", #confirmed
    "xiaomi.c107",
    "xiaomi.d101",
    "xiaomi.d102ev",
    "xiaomi.d102gl",
    "xiaomi.ov21gl",
    "xiaomi.ov21cn",
    "xiaomi.ov51gl",
    "xiaomi.pv21cn",
    "xiaomi.ov31gl",
    "xiaomi.ov42gl",
    "xiaomi.ov43gb",
    "xiaomi.ov71gl",
    "xiaomi.ov81gl",
})


def map_url_endpoint(key: str) -> str:
    """Cloud endpoint path segment for fetching a map blob.

    ijai is the only confirmed _pro user (ijai.v17 hardware-verified; Tasshack
    uses _pro when device v3 flag is True). All other brands use the plain
    variant, per PiotrMachowski's extractor (verified 2026-07-03).
    Connector falls back to the other variant on a code -8 response.
    """
    return "get_interim_file_url_pro" if key == "ijai" else "get_interim_file_url"


def parser_key(profile) -> str:
    """Which parser family decodes ``profile``'s cloud map blob.

    Untyped (duck-types `.brand`/`.profile_id`) so tests/pure can import this
    module standalone without pulling in `spec.types.ModelProfile`.
    """
    if profile.brand == "xiaomi":
        return "xiaomi" if profile.profile_id in _XIAOMI_JSON_MAP_PROFILES else "ijai"
    return profile.brand


def has_ijai_grid(brand: str) -> bool:
    """True only for ijai: its unpacked blob is the `RobotMap` protobuf the
    crisp labelled-grid extractor (`map_vector.extract_grid`) reads."""
    return brand == "ijai"


def has_json_grid(brand: str) -> bool:
    """True only for the xiaomi JSON-map family: its decrypted payload is the
    JSON object whose base64+zlib `map_data` is a labelled occupancy grid
    (`map_vector.extract_json_grid`), the second source of true room contours.
    `parser_key()` already routes only the JSON-map profiles to "xiaomi"."""
    return brand == "xiaomi"


def overlay_units_per_metre(brand: str) -> float:
    """Divisor converting this brand's overlay coordinates to metres.

    `vacuum_map_parser_xiaomi` (the JSON-map family: ov71gl, ov81gl, d109gl,
    ...) and `vacuum_map_parser_dreame` report every overlay coordinate in
    MILLIMETRES. The xiaomi family builds its coordinate transform with the
    payload `resolution` as "Millimeters per pixel" and `minX`/`minY` straight
    from the mm `origin_x`/`origin_y`, and reads the dock from the raw mm
    `pile_x`/`pile_y`; dreame reads the header positions (dock, vacuum) and
    room bboxes as raw mm — `Room` corners are `(grid + left) * pixel_size`.
    The card and `Device.zone_clean_params` both work in metres (the latter
    multiplies by 1000 for the wire), so only these families need scaling.
    Every other brand already emits metres.
    """
    return 1000.0 if brand in ("xiaomi", "dreame") else 1.0


# Model suffixes the installed vacuum_map_parser_ijai does not list as hex-key
# models but which decrypt only with the hex-style key. Each is an alias of a
# profile whose base model IS in the upstream list (b106bk -> b106eu,
# d106gl -> c101eu), see spec/registry.py.
_IJAI_EXTRA_HEX_KEY_MODELS = frozenset({"b106bk", "d106gl"})


def _patch_ijai_hex_key_models() -> None:
    """Teach vacuum_map_parser_ijai the hex-key aliases it does not know about."""
    from vacuum_map_parser_ijai import aes_decryptor

    original = aes_decryptor.is_EncryptKeyTypeHex_model
    if getattr(original, "_xiaomi_vac_patched", False):
        return

    def patched(model: str) -> bool:
        return model.split(".")[-1].lower() in _IJAI_EXTRA_HEX_KEY_MODELS or original(model)

    patched._xiaomi_vac_patched = True
    aes_decryptor.is_EncryptKeyTypeHex_model = patched


def ijai_hex_key_first(model: str) -> bool:
    """Whether the hex-style key is the first one to try for this ijai model.

    @param model: full model string, e.g. "xiaomi.vacuum.d106gl".
    @returns: True when upstream or our alias list marks it as a hex-key model.
    """
    from vacuum_map_parser_ijai import status_mapping

    suffix = model.split(".")[-1].lower()
    return suffix in _IJAI_EXTRA_HEX_KEY_MODELS or status_mapping.is_EncryptKeyTypeHex_model(model)


def ijai_decrypt(
    raw: bytes, *, wifi_sn: str, owner_id: str, device_id: str, model: str,
    device_mac: str, hex_key: bool,
) -> bytes:
    """Decrypt and decompress one ijai cloud blob with an explicit key type.

    @param raw: the downloaded blob, base64 or already binary.
    @param hex_key: True for the 32-char hex key, False for the 16-char middle slice.
    @returns: the decompressed RobotMap protobuf bytes.
    """
    import binascii

    from Crypto.Hash import MD5
    from vacuum_map_parser_ijai import aes_decryptor

    try:
        raw = base64.b64decode(raw, validate=True)
    except binascii.Error:
        pass
    mac_part = "".join(device_mac.lower().split(":"))
    suffix = model.split(".")[-1]
    if len(suffix) == 2:
        suffix = "00" + suffix
    elif len(suffix) == 3:
        suffix = "0" + suffix
    elif len(suffix) > 4:
        suffix = suffix[-4:]
    aeskey = aes_decryptor.aes_encrypt("+".join([wifi_sn, owner_id, device_id]), mac_part + suffix)
    digest = MD5.new(aeskey.encode("utf-8")).hexdigest()
    key = digest if hex_key else digest[8:-8].upper()
    return zlib.decompress(aes_decryptor.aes_decrypt(raw, key, hex_key))


def ijai_model_key_unpack(raw: bytes, model: str, device_id: str) -> bytes | str:
    """Decrypt an ijai-routed blob with the key derived from model and device id.

    @param raw: base64 text, or a JSON `{"data": "<base64>"}` wrapper of it.
    @param model: full model string, e.g. "xiaomi.vacuum.b106eu".
    @param device_id: the cloud device id.
    @returns: RobotMap protobuf bytes, or the JSON text of a Xiaomi map.
    @throws ValueError: when the payload is neither.
    """
    import json

    from .xiaomi_json_decrypt import decrypt_with_model_key

    try:
        wrapper = json.loads(raw)
    except ValueError:
        wrapper = None
    if isinstance(wrapper, dict):
        raw = wrapper["data"]
    plain = decrypt_with_model_key(base64.b64decode(raw, validate=True), model, device_id)
    if plain.startswith(b"\x08"):
        return plain
    text = plain.decode("utf-8")
    if not isinstance(json.loads(text), dict):
        raise ValueError("model-key payload is neither an ijai protobuf nor a JSON object")
    return text


def ijai_unpack_any_key(raw: bytes, **kw) -> tuple[bytes | str, str]:
    """Decrypt an ijai blob trying the likely key type first, then the other, then the model key.

    @param raw: the downloaded blob.
    @param kw: wifi_sn, owner_id, device_id, model, device_mac.
    @returns: (unpacked bytes, or JSON text from the model key; key type "hex", "text" or "model").
    """
    first = ijai_hex_key_first(kw["model"])
    errors = []
    for hex_key in (first, not first):
        try:
            return ijai_decrypt(raw, hex_key=hex_key, **kw), ("hex" if hex_key else "text")
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)
    model = kw["model"]
    if model.startswith("xiaomi.") and len(model[-16:]) == 16:
        try:
            return ijai_model_key_unpack(raw, model, kw["device_id"]), "model"
        except Exception:  # noqa: BLE001
            pass
    raise errors[0]


def make_parser(brand: str, model: str, palette, sizes, drawables, image_config, texts):
    """Construct the parser for ``brand`` (lazy import). Raises ValueError for an
    unknown brand, ImportError if the brand's dep isn't installed."""
    if brand == "ijai":
        from vacuum_map_parser_ijai.map_data_parser import IjaiMapDataParser
        _patch_ijai_hex_key_models()
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
    if brand == "roidmi":
        from vacuum_map_parser_roidmi.map_data_parser import RoidmiMapDataParser
        return RoidmiMapDataParser(palette, sizes, drawables, image_config, texts)
    raise ValueError(f"no map parser for brand {brand!r}")


def required_map_key_inputs(brand: str) -> frozenset[str]:
    """Keys that MUST be non-empty before a MapFetcher can be constructed.

    ijai   : wifi_sn + device_mac (both feed the AES-ECB key derivation)
    xiaomi : model + device_id   (AES-CBC IV derivation; both always present)
    dreame : none required        (enckey is polled from the cloud at fetch time,
                                   not from the device; plain zlib fallback for
                                   unencrypted models when no enckey is found)
    viomi  : none                 (plain zlib; no local key material needed)
    roidmi : none                 (gzip decompress; no key material needed)
    """
    if brand == "ijai":
        return frozenset({"wifi_sn", "device_mac"})
    if brand in ("xiaomi", "dreame", "viomi", "roidmi"):
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
        # enckey from siid=6/piid=3 cloud property; None for unencrypted models.
        # Only passed to parser.unpack_map for models in its built-in IVs dict.
        # Zero-IV models are decrypted by dreame_decrypt_cloud_blob before parse.
        return {"enckey": enckey} if enckey is not None else {}
    if brand in ("viomi", "roidmi"):
        return {}
    raise ValueError(f"no map parser for brand {brand!r}")
