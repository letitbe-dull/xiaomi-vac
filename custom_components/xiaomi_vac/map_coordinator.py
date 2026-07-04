"""Cloud map coordinator: periodically fetch + parse the active map."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .cloud.connector import XiaomiCloud
from .const import (
    CONF_DEVICE_ID,
    CONF_MAC,
    CONF_MODEL,
    CONF_PASS_TOKEN,
    CONF_SERVER,
    CONF_SERVICE_TOKEN,
    CONF_SSECURITY,
    CONF_USER_ID,
    CONF_USERNAME,
    CONF_WIFI_SN,
    DOMAIN,
    MAP_IDLE_INTERVAL,
    MAP_SCAN_INTERVAL,
)
from .coordinator import XiaomiVacuumCoordinator
from .device import IjaiVacuumDevice
from .map import MapFetcher, MapResult, SessionExpired
from .map_cache import MapCache
from .map_parsers import parser_key, required_map_key_inputs
from .spec.types import MapCapability

# activities where the map is actually changing
_ACTIVE = {"cleaning", "returning"}

# Cloud upload slots tried every cycle for the active map (per map-reliability
# Phase 0: divergence between them is real but not gated on any local state,
# so both are read every poll rather than treating one as a fallback).
_SLOTS = ("0", "1")

# Cache key for devices whose map capability has no map-list catalogue at all
# (single physical map, e.g. viomi/dreame/roidmi profiles without get_map_list).
# There is exactly one map, so a fixed key round-trips the cache correctly.
_SINGLE_MAP_ID = 0

# Vector keys that only make sense for the CURRENTLY active map; stripped from
# any other cached map shown in `.maps` so its stale position doesn't render on
# the wrong floor plan (map-reliability Phase 2: live-overlay scoping).
_LIVE_ONLY_VECTOR_KEYS = ("path", "vacuum", "goto", "vacuum_room", "vacuum_room_name")

# Phase-4 "Refresh map" window: upper bound before the vacuum is sent home,
# even if no fresh Key-A blob has arrived (map-reliability Phase 0 decision).
_REFRESH_TIMEOUT = 15.0
# How often we re-poll the cloud during the refresh window; loose enough not to
# hammer the API, tight enough to catch a fresh upload within the 15 s budget.
_REFRESH_POLL_INTERVAL = 3.0
# Activities in which starting a clean would be destructive/wrong. "None" is
# treated as unknown -> refuse rather than move the vacuum on a hunch.
_REFRESH_ELIGIBLE = {"docked", "idle"}

_LOGGER = logging.getLogger(__name__)

_ISSUE_MAP_ENCRYPTED = "map_encrypted"
_ISSUE_MAP_REFRESHING = "map_refreshing"


def _repair_issue_id(entry_id: str) -> str:
    return f"{_ISSUE_MAP_ENCRYPTED}_{entry_id}"


def _refreshing_issue_id(entry_id: str) -> str:
    return f"{_ISSUE_MAP_REFRESHING}_{entry_id}"


def _static_only(vector: dict) -> dict:
    return {k: v for k, v in vector.items() if k not in _LIVE_ONLY_VECTOR_KEYS}


class XiaomiMapCoordinator(DataUpdateCoordinator[MapResult]):
    """Holds a logged-in cloud session and refreshes the map."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: IjaiVacuumDevice,
        control: XiaomiVacuumCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:map:{entry.data[CONF_MODEL]}",
            update_interval=timedelta(seconds=MAP_IDLE_INTERVAL),
        )
        self.entry = entry
        self._device = device
        self._control = control
        self._cloud: XiaomiCloud | None = None
        self._fetcher: MapFetcher | None = None
        self._cache: MapCache | None = None
        # Whether this profile has a map-list catalogue at all (set in _build).
        # A device without one has exactly one physical map -> _SINGLE_MAP_ID.
        self._has_map_list = False
        # Phase-4 "Refresh map" single-flight lock + observable flag.
        self._refresh_lock = asyncio.Lock()
        self._refreshing = False

    @property
    def device(self) -> IjaiVacuumDevice:
        return self._device

    @property
    def control(self) -> XiaomiVacuumCoordinator:
        return self._control

    @property
    def refreshing(self) -> bool:
        """True while a Phase-4 refresh cycle is undocking the vacuum."""
        return self._refreshing

    def _tune_interval(self) -> None:
        """Poll fast while the vacuum is moving, slowly when docked/idle."""
        data = self._control.data
        active = bool(data and data.activity in _ACTIVE)
        secs = MAP_SCAN_INTERVAL if active else MAP_IDLE_INTERVAL
        new = timedelta(seconds=secs)
        if self.update_interval != new:
            self.update_interval = new

    def _build(self) -> MapFetcher:
        """Blocking: restore the saved cloud session and construct a fetcher.

        wifi_sn and mac are read LIVE from the device (the source of truth for
        the map AES key); the values stashed at setup time can be empty/stale.
        """
        d = self.entry.data
        # No password: the saved session is restored and renewed via passToken.
        cloud = XiaomiCloud(d[CONF_USERNAME])
        cloud.restore_session(d[CONF_USER_ID], d[CONF_SSECURITY], d[CONF_SERVICE_TOKEN],
                              d.get(CONF_PASS_TOKEN))
        self._cloud = cloud

        brand = parser_key(self._device.profile)
        required = required_map_key_inputs(brand)

        cap = self._device.profile.map
        self._has_map_list = isinstance(cap, MapCapability) and cap.get_map_list is not None

        wifi_sn = ""
        mac = ""
        if required:
            live_sn = self._device.get_wifi_sn(d[CONF_USER_ID])
            live_mac = self._device.get_mac()
            wifi_sn = live_sn or d.get(CONF_WIFI_SN) or ""
            mac = live_mac or d.get(CONF_MAC) or ""

        _LOGGER.debug(
            "Map key inputs: brand=%s wifi_sn set=%s mac=%s user_id=%s device_id=%s model=%s",
            brand, bool(wifi_sn), mac, d[CONF_USER_ID], d[CONF_DEVICE_ID], d[CONF_MODEL],
        )
        if "wifi_sn" in required and not wifi_sn:
            raise UpdateFailed("Could not read wifi_sn from the vacuum (needed to decrypt map)")
        if "device_mac" in required and not mac:
            # An empty mac silently produces the wrong AES key (decrypt fails
            # with "Padding is incorrect"), so refuse to build with one.
            raise UpdateFailed("Could not read mac from the vacuum (needed to decrypt map)")

        return MapFetcher(
            cloud,
            server=d[CONF_SERVER],
            user_id=d[CONF_USER_ID],
            device_id=d[CONF_DEVICE_ID],
            model=d[CONF_MODEL],
            mac=mac,
            wifi_sn=wifi_sn,
            parser_brand=brand,
        )

    async def _ensure_cache(self) -> MapCache:
        if self._cache is None:
            cache = MapCache(self.hass, self.entry.entry_id)
            await cache.async_load()
            self._cache = cache
        return self._cache

    def _fetch_slots(self) -> list[MapResult | None]:
        """Blocking: try every cloud slot for the active map this cycle.

        A raised SessionExpired from an earlier slot short-circuits the rest —
        the whole session is dead, not just this slot's blob.
        """
        return [self._fetcher.fetch(slot) for slot in _SLOTS]

    def _resolve_active_id(
        self, active_meta: dict | None, decoded: list[MapResult], maps_meta: list[dict],
    ) -> int | None:
        """Which map this cycle's data belongs to, in order of trust:

        1. A live blob's own embedded id (ground truth from the cloud blob
           itself — ijai only; other brands never carry one).
        2. The local map-list's "cur" entry (independent of the cloud fetch).
        3. A fixed single-map key, but ONLY when this profile has no map-list
           capability at all — an empty `maps_meta` from a transient read
           failure on a multi-map device must NOT be mistaken for that.
        """
        for r in decoded:
            if r.map_id is not None:
                return r.map_id
        if active_meta and active_meta.get("id") is not None:
            try:
                return int(active_meta["id"])
            except (TypeError, ValueError):
                pass
        if not self._has_map_list and not maps_meta:
            return _SINGLE_MAP_ID
        return None

    def _serve(
        self, cache: MapCache, active_id: int | None, maps_meta: list[dict],
    ) -> MapResult | None:
        """Build the result to serve from whatever the cache now holds for
        `active_id` (a live decode this cycle was already upserted before this
        runs, so cache and live are never out of sync — serve-parity holds by
        construction). None only when nothing has ever been cached for it.
        """
        if active_id is None:
            return None
        entry = cache.get(active_id)
        if entry is None:
            return None
        name_by_id = {
            int(m["id"]): m.get("name") for m in maps_meta if m.get("id") is not None
        }
        served = MapResult(
            image_png=entry.png,
            attributes=entry.attributes,
            vector=entry.vector,
            map_id=active_id,
            content_hash=entry.content_hash,
        )
        served.maps = [
            {
                **(cached.vector if map_id == active_id else _static_only(cached.vector)),
                "map_id": map_id,
                "map_name": name_by_id.get(map_id),
                "active": map_id == active_id,
            }
            for map_id, cached in cache.all().items()
        ]
        return served

    async def _async_update_data(self) -> MapResult:
        self._tune_interval()
        try:
            if self._fetcher is None:
                self._fetcher = await self.hass.async_add_executor_job(self._build)
            cache = await self._ensure_cache()

            # The map list (distinct physical maps) drives multi-map; best-effort
            # so a flaky local read never blocks the active-map fetch.
            try:
                maps_meta = await self.hass.async_add_executor_job(self._device.map_list)
            except Exception:  # noqa: BLE001
                maps_meta = []

            try:
                slot_results = await self.hass.async_add_executor_job(self._fetch_slots)
            except SessionExpired:
                # Token likely expired — renew it with the passToken and retry.
                # If renewal fails, the passToken is dead too: ask the user to
                # re-auth (raises a reauth flow) rather than dead-end.
                if not await self._refresh_and_persist():
                    raise ConfigEntryAuthFailed("Cloud session expired") from None
                slot_results = await self.hass.async_add_executor_job(self._fetch_slots)

            decoded = [r for r in slot_results if r is not None]
            active_meta = next((m for m in maps_meta if m.get("cur")), None)
            active_id = self._resolve_active_id(active_meta, decoded, maps_meta)

            # Whichever slot decrypted (Key A) wins and refreshes the cache for
            # this map id; both slots being None just means both were Key B this
            # cycle — normal, not an error, and handled by serving from cache.
            live = decoded[0] if decoded else None
            if live is not None and active_id is not None:
                await cache.async_upsert(
                    active_id,
                    png=live.image_png,
                    attributes=live.attributes,
                    vector=live.vector,
                    content_hash=live.content_hash,
                    timestamp=time.time(),
                )

            # Prune maps the device no longer lists, but never off a transient
            # empty read (map-reliability Phase 0 eviction decision).
            if maps_meta:
                keep_ids = {int(m["id"]) for m in maps_meta if m.get("id") is not None}
                if keep_ids:
                    await cache.async_prune(keep_ids)

            result = self._serve(cache, active_id, maps_meta)
            if result is None:
                # Nothing live AND nothing cached for this map id. Usually a
                # brand-new map that's never rendered readably yet, but it's
                # ALSO what a fetcher built with stale key inputs produces —
                # e.g. wifi_sn/mac read while the device was briefly
                # unreachable at startup. Drop the fetcher so the next cycle
                # re-reads live values and self-heals once the device is back.
                self._fetcher = None
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    _repair_issue_id(self.entry.entry_id),
                    is_fixable=True,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=_ISSUE_MAP_ENCRYPTED,
                    data={"entry_id": self.entry.entry_id},
                )
                raise UpdateFailed(
                    "No readable map available (device-side encrypted upload, "
                    "and no cached copy yet)"
                )
            ir.async_delete_issue(self.hass, DOMAIN, _repair_issue_id(self.entry.entry_id))
            return result
        except (UpdateFailed, ConfigEntryAuthFailed):
            raise
        except SessionExpired:
            raise ConfigEntryAuthFailed("Cloud session expired") from None
        except Exception as err:  # noqa: BLE001
            self._fetcher = None
            raise UpdateFailed(f"Map update error: {err}") from err

    async def async_refresh_map_undock(self) -> bool:
        """Briefly undock the vacuum to force a fresh readable ("Key A") upload.

        Returns True if a new render for the active map landed within the window,
        False if the window timed out, the vacuum was ineligible, or another
        refresh was already in progress. Always returns the vacuum to the dock
        when a clean was actually started.

        Callers MUST have obtained explicit user consent — this method moves the
        physical vacuum. Guards inside are the last line of defence, not the
        consent gate.
        """
        if self._refresh_lock.locked():
            _LOGGER.info("Refresh-map: already running; ignoring duplicate press")
            return False

        async with self._refresh_lock:
            ctrl_data = self._control.data
            activity = ctrl_data.activity if ctrl_data is not None else None
            if activity not in _REFRESH_ELIGIBLE:
                _LOGGER.warning(
                    "Refresh-map: vacuum is %s, not docked/idle; refusing to move",
                    activity,
                )
                return False

            cache = await self._ensure_cache()
            active_id = self.data.map_id if self.data is not None else None
            prior_hash: str | None = None
            if active_id is not None:
                entry = cache.get(active_id)
                prior_hash = entry.content_hash if entry is not None else None

            self._refreshing = True
            self.async_update_listeners()
            # Swap any "map encrypted" notice for a "refreshing…" notice so the
            # user watching the repair panel sees the action is in progress.
            # Deleting the old id first avoids two overlapping notices for the
            # same underlying condition.
            ir.async_delete_issue(
                self.hass, DOMAIN, _repair_issue_id(self.entry.entry_id)
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                _refreshing_issue_id(self.entry.entry_id),
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=_ISSUE_MAP_REFRESHING,
            )
            started = False
            try:
                try:
                    await self.hass.async_add_executor_job(self._device.start)
                    started = True
                except Exception as ex:  # noqa: BLE001
                    _LOGGER.error("Refresh-map: start-sweep failed: %s", ex)
                    return False

                fresh = await self._await_fresh_render(cache, active_id, prior_hash)
                return fresh
            finally:
                if started:
                    try:
                        await self.hass.async_add_executor_job(self._device.return_home)
                    except Exception as ex:  # noqa: BLE001
                        _LOGGER.error("Refresh-map: return-to-dock failed: %s", ex)
                self._refreshing = False
                self.async_update_listeners()
                # Clear the "refreshing…" notice unconditionally; the normal
                # poll cycle will re-raise `map_encrypted` on the next run if
                # no readable map exists after the undock attempt.
                ir.async_delete_issue(
                    self.hass, DOMAIN, _refreshing_issue_id(self.entry.entry_id)
                )
                # Nudge status so the vacuum entity flips back to docked promptly.
                await self._control.async_request_refresh()

    async def _await_fresh_render(
        self,
        cache: MapCache,
        active_id: int | None,
        prior_hash: str | None,
    ) -> bool:
        """Poll the coordinator until the active map's cache hash changes or the
        timeout elapses. Returns True on hash change (fresh render captured)."""
        deadline = time.monotonic() + _REFRESH_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(_REFRESH_POLL_INTERVAL)
            try:
                await self.async_refresh()
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Refresh-map poll: coordinator refresh error: %s", ex)
                continue
            current_id = active_id
            if current_id is None and self.data is not None:
                current_id = self.data.map_id
            if current_id is None:
                continue
            entry = cache.get(current_id)
            if entry is not None and entry.content_hash != prior_hash:
                return True
        return False

    async def _refresh_and_persist(self) -> bool:
        """Renew the cloud session via passToken and save the new tokens."""
        if self._cloud is None:
            return False
        if not await self.hass.async_add_executor_job(self._cloud.refresh):
            return False
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                CONF_SERVICE_TOKEN: self._cloud.service_token,
                CONF_SSECURITY: self._cloud.ssecurity,
                CONF_PASS_TOKEN: self._cloud.pass_token or "",
            },
        )
        _LOGGER.info("Renewed Xiaomi cloud session via passToken")
        return True
