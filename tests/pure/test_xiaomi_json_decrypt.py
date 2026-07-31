"""Tests for xiaomi_json_decrypt: the version-2 Xiaomi cloud map blob decoder.

Reverse-engineered 2026-07-31 from the real Xiaomi Home Android app's RN
plugin bundle (see BRIEF.md, round 9). vacuum_map_parser_xiaomi's own
decrypt() is broken for this whole model family: every profile in
map_parsers._XIAOMI_JSON_MAP_PROFILES has a 20-char MIoT model string, and
the upstream package uses that full string directly as the AES key
(pycryptodome rejects any non-16/24/32-byte key). The real app derives the
key from only the model string's last 16 characters, and the endpoint wraps
the ciphertext in a `{"version":2,"data":"<base64>"}` envelope the upstream
package never unwraps either.

These tests round-trip synthetic data through a from-scratch reimplementation
of the app's encrypt side (independent of the production decrypt code, so it
can't share a bug with what it's testing) rather than embedding the real
captured blob from a live device, which is private user data (a floor plan
of their home) that doesn't belong in a public test fixture.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zlib

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from xiaomi_json_decrypt import decrypt_xiaomi_json_map

# Loaded via the synthetic `xvac` package (see tests/pure/conftest.py) — map.py
# has relative imports so it can't be imported standalone.
from xvac.map import MapFetcher

_IV = b"ABCDEF1234123412"


def _encrypt_like_the_real_app(payload: dict, *, model: str, device_id: str) -> bytes:
    """Mirror of the app's map.js encrypt path: the inverse of decrypt_xiaomi_json_map."""
    model_key = model[-16:].encode("latin1")
    original_work = model_key + device_id.encode("latin1")
    enc_key = AES.new(model_key, AES.MODE_CBC, _IV).encrypt(pad(original_work, AES.block_size))
    encrypt_key = hashlib.md5(enc_key).digest()

    plaintext = zlib.compress(json.dumps(payload).encode("utf-8"))
    ciphertext = AES.new(encrypt_key, AES.MODE_CBC, _IV).encrypt(pad(plaintext, AES.block_size))
    envelope = {"version": 2, "data": base64.b64encode(ciphertext).decode("ascii")}
    return json.dumps(envelope).encode("utf-8")


@pytest.mark.parametrize("model", [
    "xiaomi.vacuum.ov42gl",   # 21 chars, the model that motivated this fix
    "xiaomi.vacuum.ov21gl",   # 21 chars, an already-"working" (never actually tested) profile
    "1234567890123456",       # exactly 16 chars: slice(-16) is a no-op
])
def test_decrypt_round_trip(model):
    payload = {"map_id": 4, "height": 160, "width": 256, "map_data": "abc123"}
    blob = _encrypt_like_the_real_app(payload, model=model, device_id="1234567890")
    result = json.loads(decrypt_xiaomi_json_map(blob, model, "1234567890"))
    assert result == payload


def test_decrypt_uses_only_last_16_chars_of_model():
    """Two model strings sharing the same last 16 chars must decrypt identically —
    proves the fix truncates rather than hashing/padding the full string."""
    payload = {"ok": True}
    device_id = "1234567890"
    long_model = "xiaomi.vacuum.ov42gl"
    same_suffix_model = "zzzzz" + long_model[-16:]
    assert long_model[-16:] == same_suffix_model[-16:]

    blob = _encrypt_like_the_real_app(payload, model=long_model, device_id=device_id)
    result = json.loads(decrypt_xiaomi_json_map(blob, same_suffix_model, device_id))
    assert result == payload


def test_decrypt_rejects_wrong_version():
    envelope = json.dumps({"version": 1, "data": "irrelevant"}).encode("utf-8")
    with pytest.raises(ValueError, match="unsupported xiaomi map blob version"):
        decrypt_xiaomi_json_map(envelope, "xiaomi.vacuum.ov42gl", "1234567890")


def test_decrypt_rejects_non_json_input():
    with pytest.raises(json.JSONDecodeError):
        decrypt_xiaomi_json_map(b"not json at all", "xiaomi.vacuum.ov42gl", "1234567890")


def test_decrypt_rejects_wrong_key_material():
    """A blob encrypted for one device must not silently decrypt for another —
    either PKCS7 unpadding or the trailing zlib decompress must reject it."""
    payload = {"map_id": 1}
    blob = _encrypt_like_the_real_app(payload, model="xiaomi.vacuum.ov42gl", device_id="1234567890")
    with pytest.raises(Exception):  # noqa: B017 - either ValueError (unpad) or zlib.error
        decrypt_xiaomi_json_map(blob, "xiaomi.vacuum.ov42gl", "9999999999")


# --- MapFetcher integration: _unpack must bypass the broken upstream
# decrypt() entirely for the "xiaomi" brand, and the result must be usable
# downstream (parser.parse() wants str/dict; MapFetcher.fetch() also hashes
# it with hashlib.sha256, which only accepts bytes) ------------------------
class FakeCloud:
    def __init__(self, *, url="http://x", blob=b""):
        self.url = url
        self.blob = blob

    def map_url(self, server, did, slot, endpoint):
        return self.url

    def download(self, url):
        return self.blob


def test_mapfetcher_unpack_xiaomi_bypasses_upstream_decrypt():
    """_unpack must never call parser.unpack_map for brand=='xiaomi' — that
    upstream path is what's broken (see module docstring)."""
    model = "xiaomi.vacuum.ov42gl"
    payload = {"map_id": 4, "height": 160, "width": 256}
    blob = _encrypt_like_the_real_app(payload, model=model, device_id="1234567890")

    fetcher = MapFetcher(
        FakeCloud(), server="de", user_id="1", device_id="1234567890", model=model,
        mac="AA:BB:CC:DD:EE:FF", wifi_sn="SN", parser_brand="xiaomi")

    def _boom(*a, **kw):
        raise AssertionError("upstream vacuum_map_parser_xiaomi.unpack_map must not be called")
    fetcher._parser.unpack_map = _boom

    unpacked = fetcher._unpack(blob)
    assert isinstance(unpacked, str)
    assert json.loads(unpacked) == payload


def test_mapfetcher_fetch_full_chain_xiaomi_json():
    """End-to-end: fetch() -> decrypt -> parser.parse() -> content_hash, no crash.

    Regression guard for the latent `hashlib.sha256(str)` TypeError: xiaomi's
    _unpack returns a JSON *string* (matching parser.parse()'s str/dict-only
    contract), which used to reach the bare `hashlib.sha256(unpacked)` call
    unencoded.
    """
    model = "xiaomi.vacuum.ov42gl"
    payload = {
        "map_id": 4, "map_type": 1, "height": 4, "width": 4,
        "resolution": 50, "origin_x": 0, "origin_y": 0,
        "map_data": base64.b64encode(zlib.compress(bytes([1] * 16))).decode("ascii"),
    }
    blob = _encrypt_like_the_real_app(payload, model=model, device_id="1234567890")

    fetcher = MapFetcher(
        FakeCloud(blob=blob), server="de", user_id="1", device_id="1234567890", model=model,
        mac="AA:BB:CC:DD:EE:FF", wifi_sn="SN", parser_brand="xiaomi")

    result = fetcher.fetch()
    # Whatever the parser/vector layer makes of this minimal synthetic frame,
    # it must not raise — that's the regression this test guards against.
    # A None result (parser rejected the frame / empty image) is an
    # acceptable outcome here since the payload is synthetic, not a real map.
    if result is not None:
        assert isinstance(result.content_hash, str)
        assert len(result.content_hash) == 64  # sha256 hex digest
