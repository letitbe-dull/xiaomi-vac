"""Pure-tier tests for the per-map cache (Phase 1 of map-reliability).

No real homeassistant import — Store is replaced by FakeStore (helpers.py)
so this runs on native Windows like the rest of tests/pure.
"""
from __future__ import annotations

import asyncio

import pytest

from .helpers import load_map_cache_module


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def cache_module(monkeypatch: pytest.MonkeyPatch):
    module, fake_store = load_map_cache_module(monkeypatch)
    return module, fake_store


def _make_cache(cache_module, entry_id="entry1"):
    module, _ = cache_module
    return module.MapCache(hass=object(), entry_id=entry_id)


def test_empty_cache_get_returns_none(cache_module) -> None:
    cache = _make_cache(cache_module)
    _run(cache.async_load())
    assert cache.get(1782117981) is None
    assert cache.all() == {}


def test_upsert_writes_and_get_roundtrips(cache_module) -> None:
    cache = _make_cache(cache_module)
    _run(cache.async_load())

    wrote = _run(cache.async_upsert(
        1782117981, png=b"\x89PNG...", attributes={"a": 1}, vector={"map_id": 1782117981},
        content_hash="hash-a", timestamp=1000.0,
    ))
    assert wrote is True

    entry = cache.get(1782117981)
    assert entry is not None
    assert entry.png == b"\x89PNG..."
    assert entry.attributes == {"a": 1}
    assert entry.content_hash == "hash-a"
    assert entry.timestamp == 1000.0


def test_upsert_same_hash_is_noop(cache_module) -> None:
    cache = _make_cache(cache_module)
    _run(cache.async_load())

    _run(cache.async_upsert(
        1, png=b"one", attributes={}, vector={}, content_hash="same", timestamp=1.0,
    ))
    wrote_again = _run(cache.async_upsert(
        1, png=b"one-but-different-bytes", attributes={"x": 2}, vector={},
        content_hash="same", timestamp=2.0,
    ))
    assert wrote_again is False
    # Entry must be untouched by the no-op poll.
    entry = cache.get(1)
    assert entry.png == b"one"
    assert entry.attributes == {}
    assert entry.timestamp == 1.0


def test_upsert_changed_hash_overwrites(cache_module) -> None:
    cache = _make_cache(cache_module)
    _run(cache.async_load())

    _run(cache.async_upsert(1, png=b"v1", attributes={}, vector={}, content_hash="h1", timestamp=1.0))
    wrote = _run(cache.async_upsert(1, png=b"v2", attributes={}, vector={}, content_hash="h2", timestamp=2.0))
    assert wrote is True
    entry = cache.get(1)
    assert entry.png == b"v2"
    assert entry.content_hash == "h2"


def test_persistence_across_instances(cache_module) -> None:
    """A second MapCache with the same entry_id sees what the first saved
    (simulates surviving a restart, since FakeStore backs the same file)."""
    module, _ = cache_module
    cache1 = module.MapCache(hass=object(), entry_id="shared")
    _run(cache1.async_load())
    _run(cache1.async_upsert(
        7, png=b"png-bytes", attributes={"rooms": []}, vector={"map_id": 7},
        content_hash="h", timestamp=5.0,
    ))

    cache2 = module.MapCache(hass=object(), entry_id="shared")
    _run(cache2.async_load())
    entry = cache2.get(7)
    assert entry is not None
    assert entry.png == b"png-bytes"
    assert entry.vector == {"map_id": 7}


def test_different_entry_ids_are_isolated(cache_module) -> None:
    module, _ = cache_module
    cache_a = module.MapCache(hass=object(), entry_id="a")
    cache_b = module.MapCache(hass=object(), entry_id="b")
    _run(cache_a.async_load())
    _run(cache_b.async_load())
    _run(cache_a.async_upsert(1, png=b"a", attributes={}, vector={}, content_hash="h", timestamp=1.0))
    assert cache_b.get(1) is None


def test_corrupted_storage_file_falls_back_to_empty(cache_module) -> None:
    module, fake_store = cache_module
    fake_store.backing["xiaomi_vac_map_cache_corrupt"] = {"not": "the expected shape"}
    cache = module.MapCache(hass=object(), entry_id="corrupt")
    _run(cache.async_load())
    assert cache.get(1) is None
    assert cache.all() == {}
    assert cache.loaded is True


def test_schema_version_mismatch_discards_cache(cache_module) -> None:
    module, fake_store = cache_module
    fake_store.backing["xiaomi_vac_map_cache_oldschema"] = {
        "version": module.STORAGE_VERSION + 1,
        "maps": {"1": {"map_head_id": 1, "png_b64": "AAAA", "content_hash": "h", "timestamp": 1.0}},
    }
    cache = module.MapCache(hass=object(), entry_id="oldschema")
    _run(cache.async_load())
    assert cache.get(1) is None


def test_malformed_individual_entry_is_skipped_not_fatal(cache_module) -> None:
    module, fake_store = cache_module
    fake_store.backing["xiaomi_vac_map_cache_partial"] = {
        "version": module.STORAGE_VERSION,
        "maps": {
            "1": {"map_head_id": 1, "png_b64": "not-valid-base64!!", "content_hash": "h", "timestamp": 1.0},
            "2": module.CachedMap(
                map_head_id=2, png=b"ok", attributes={}, vector={}, content_hash="h2", timestamp=2.0,
            ).to_json(),
        },
    }
    cache = module.MapCache(hass=object(), entry_id="partial")
    _run(cache.async_load())
    assert cache.get(1) is None
    entry2 = cache.get(2)
    assert entry2 is not None
    assert entry2.png == b"ok"


def test_load_raising_store_falls_back_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    module, fake_store = load_map_cache_module(monkeypatch)

    class ExplodingStore(fake_store):
        async def async_load(self):
            raise OSError("disk read failed")

    monkeypatch.setattr(
        __import__("sys").modules["homeassistant.helpers.storage"], "Store", ExplodingStore,
    )
    # map_cache already imported Store by reference at module import time, so
    # patch the name it actually holds.
    monkeypatch.setattr(module, "Store", ExplodingStore)

    cache = module.MapCache(hass=object(), entry_id="explode")
    _run(cache.async_load())
    assert cache.loaded is True
    assert cache.get(1) is None


def test_all_returns_copy_not_live_dict(cache_module) -> None:
    cache = _make_cache(cache_module)
    _run(cache.async_load())
    _run(cache.async_upsert(1, png=b"a", attributes={}, vector={}, content_hash="h", timestamp=1.0))
    snapshot = cache.all()
    snapshot.pop(1)
    assert cache.get(1) is not None


# ---------------------------------------------------------------------------
# Phase 5 golden path: Key A seeds cache → Key B cycle → restart persistence
# ---------------------------------------------------------------------------


def test_golden_path_key_a_seeds_cache_key_b_served_from_cache(cache_module) -> None:
    """Full golden path for map-reliability:

    Cycle 1 — Key A decrypts → coordinator calls async_upsert → cache populated.
    Cycle 2 — Key B (encrypted blob, coordinator skips upsert) → cache unchanged.
    Restart  — new MapCache with same entry_id recovers the render via async_load
               before any cloud fetch, satisfying the restart-persistence criterion.
    """
    module, _ = cache_module
    # Use the real map id from the plan's capture data as a realistic sentinel.
    MAP_ID = 1782117981  # "Ground Floor" id from capture_20260704_142618.csv

    # Cycle 1: Key A decrypts → coordinator calls async_upsert.
    cache = module.MapCache(hass=object(), entry_id="golden")
    _run(cache.async_load())
    wrote = _run(cache.async_upsert(
        MAP_ID,
        png=b"ground-floor-key-a-render",
        attributes={"rooms": 3, "area": 25.5},
        vector={"map_id": MAP_ID},
        content_hash="hash-key-a",
        timestamp=1000.0,
    ))
    assert wrote is True

    # Cycle 2: Key B — coordinator receives None from both slots and calls no
    # upsert. The cache must retain the Key-A render without any change.
    entry = cache.get(MAP_ID)
    assert entry is not None
    assert entry.png == b"ground-floor-key-a-render"
    assert entry.content_hash == "hash-key-a"
    assert entry.attributes == {"rooms": 3, "area": 25.5}

    # Simulate HA restart: a fresh MapCache instance (same entry_id) must
    # recover the cached render immediately on async_load(), before any fetch.
    cache_restarted = module.MapCache(hass=object(), entry_id="golden")
    _run(cache_restarted.async_load())
    assert cache_restarted.loaded is True
    entry_after = cache_restarted.get(MAP_ID)
    assert entry_after is not None
    assert entry_after.png == b"ground-floor-key-a-render"
    assert entry_after.content_hash == "hash-key-a"
