"""Authenticated HTTP endpoint that serves the vector map to the card.

The vector contract (grid RLE + overlays, ~20 KB) is too big to publish as a
state attribute (it would bloat the recorder and the state machine), so the card
fetches it on demand from here instead. Auth is required: the HA frontend sends
the user's token automatically.
"""
from __future__ import annotations

from http import HTTPStatus

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


class VectorMapView(HomeAssistantView):
    """GET /api/xiaomi_vac/map/{target} -> latest vector map JSON.

    `target` may be a config entry_id OR an entity_id (e.g. vacuum.kevin_jonas);
    the entity_id is resolved to its config entry here so the card doesn't have
    to dig the entry id out of the frontend registry.
    """

    url = "/api/xiaomi_vac/map/{target}"
    name = "api:xiaomi_vac:map"
    requires_auth = True

    async def get(self, request, target: str):
        hass: HomeAssistant = request.app["hass"]
        # `target` is a config entry_id, or an entity_id we resolve to its entry.
        entry_id = target
        if "." in target:
            ent = er.async_get(hass).async_get(target)
            entry_id = ent.config_entry_id if ent else None
        entry = (
            hass.config_entries.async_get_entry(entry_id) if entry_id else None
        )
        runtime = getattr(entry, "runtime_data", None) if entry else None
        coordinator = runtime.map if runtime else None
        if coordinator is None or coordinator.data is None:
            return self.json_message("no map available", HTTPStatus.NOT_FOUND)
        data = coordinator.data
        # `maps` is the list-based contract (one entry per physical map, each a
        # vector tagged map_id/map_name/active). Fall back to wrapping the single
        # active vector if an older MapResult somehow lacks it.
        maps = data.maps or [{**data.vector, "active": True}]
        return self.json({"maps": maps})
