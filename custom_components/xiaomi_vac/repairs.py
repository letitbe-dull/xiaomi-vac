"""Repair flow for the encrypted-map notice (map-reliability Phase 4)."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class MapEncryptedRepairFlow(RepairsFlow):
    """Confirm and launch the "Refresh map" undock sequence from the repair notice.

    The vacuum will physically move, so this flow shows the same warning wording
    as the button entity before doing anything.
    """

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        # A gone entry can't produce a coordinator; close the flow cleanly rather
        # than leaving the user stuck in a dead confirm dialog.
        if entry is None or entry.runtime_data is None or entry.runtime_data.map is None:
            _LOGGER.warning(
                "map_encrypted repair flow: entry %s has no map coordinator", self._entry_id
            )
            return self.async_create_entry(title="", data={})

        # Fire-and-forget: the undock cycle can take up to ~15s and we don't
        # want the flow dialog to block on it. The coordinator handles guards,
        # single-flight, and return-to-dock internally.
        self.hass.async_create_task(entry.runtime_data.map.async_refresh_map_undock())
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant,  # noqa: ARG001
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Build the fix flow for an issue this integration raised."""
    if issue_id.startswith("map_encrypted_") and data:
        entry_id = data.get("entry_id")
        if isinstance(entry_id, str):
            return MapEncryptedRepairFlow(entry_id)
    # No handler matched: return a no-op confirm so HA doesn't crash.
    _LOGGER.debug("No fix flow matched issue %s (domain %s)", issue_id, DOMAIN)
    return _NoopRepairFlow()


class _NoopRepairFlow(RepairsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return self.async_create_entry(title="", data={})
