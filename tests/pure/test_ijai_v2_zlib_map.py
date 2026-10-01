"""Raw-zlib map decoding for the explicitly verified ijai.v2 profile."""
from __future__ import annotations

import zlib

from xvac.map import MapFetcher


class FakeCloud:
    def __init__(self, blob: bytes):
        self.blob = blob

    def map_url(self, server, did, slot, endpoint):
        return "http://map"

    def download(self, url):
        return self.blob


class RecorderParser:
    def __init__(self, unpacked: bytes = b"unpacked"):
        self.unpacked = unpacked
        self.calls = []

    def unpack_map(self, raw, **kwargs):
        self.calls.append((raw, kwargs))
        return self.unpacked


def _fetcher(blob: bytes, model: str = "ijai.vacuum.v2") -> MapFetcher:
    return MapFetcher(
        FakeCloud(blob),
        server="de",
        user_id="owner",
        device_id="device",
        model=model,
        mac="AA:BB:CC:DD:EE:FF",
        wifi_sn="wifi",
        parser_brand="ijai",
    )


def test_v2_raw_zlib_protobuf_bypasses_encrypted_unpack():
    protobuf = b"\x08synthetic protobuf"
    compressed = zlib.compress(protobuf)
    assert compressed.startswith(b"\x78\x9c")

    fetcher = _fetcher(compressed)
    parser = RecorderParser()
    fetcher._parser = parser

    assert fetcher._unpack(compressed) == protobuf
    assert parser.calls == []


def test_v2_corrupt_raw_zlib_returns_none():
    fetcher = _fetcher(b"\x78\x9ccorrupt")

    assert fetcher.fetch() is None


def test_v2_raw_zlib_with_non_protobuf_prefix_returns_none():
    fetcher = _fetcher(zlib.compress(b"\x0anot an ijai frame"))

    assert fetcher.fetch() is None


def test_v2_encrypted_blob_still_uses_existing_unpack_path():
    encrypted = b"encrypted map blob"
    fetcher = _fetcher(encrypted)
    parser = RecorderParser()
    fetcher._parser = parser

    assert fetcher._unpack(encrypted) == b"unpacked"
    assert parser.calls == [
        (
            encrypted,
            {
                "wifi_sn": "wifi",
                "owner_id": "owner",
                "device_id": "device",
                "model": "ijai.vacuum.v2",
                "device_mac": "AA:BB:CC:DD:EE:FF",
            },
        )
    ]


def test_other_ijai_profile_does_not_accept_raw_zlib_directly():
    compressed = zlib.compress(b"\x08synthetic protobuf")
    fetcher = _fetcher(compressed, model="ijai.vacuum.v17")
    parser = RecorderParser()
    fetcher._parser = parser

    assert fetcher._unpack(compressed) == b"unpacked"
    assert parser.calls[0][0] == compressed
