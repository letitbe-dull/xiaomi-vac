"""Config-entry diagnostics for the Xiaomi Vacuum integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import XiaomiConfigEntry
from .const import CONF_MODEL, CONF_SERVER

_NOT_REDACTED = frozenset({CONF_MODEL, CONF_SERVER})


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: XiaomiConfigEntry
) -> dict[str, Any]:
    """Return the entry data, redacted except model and server, and the last map cycle record.

    @param hass: the Home Assistant instance.
    @param entry: the config entry diagnostics were requested for.
    @returns: {"entry_data": dict, "map_cycle": dict | None}.
    """
    runtime = getattr(entry, "runtime_data", None)
    map_coordinator = runtime.map if runtime is not None else None
    cycle = map_coordinator.last_cycle if map_coordinator is not None else None
    return {
        "entry_data": async_redact_data(entry.data, entry.data.keys() - _NOT_REDACTED),
        "map_cycle": cycle.as_dict() if cycle is not None else None,
    }
