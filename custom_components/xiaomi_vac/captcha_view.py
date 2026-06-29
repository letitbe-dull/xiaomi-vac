"""Serves login images (captcha, QR) same-origin for the config-flow dialog.

HA config-flow markdown strips data: URIs and the translation validator rejects
custom tags like <ha-qr-code> (INVALID_TAG). But a plain <img> with an http src
from a same-origin endpoint renders fine, so we stash the PNG bytes in
hass.data and serve them here. These images aren't sensitive, so no auth.
"""
from __future__ import annotations

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN

IMG_URL = "/api/xiaomi_vac/img"


class ImageView(HomeAssistantView):
    url = IMG_URL
    name = "api:xiaomi_vac:img"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        name = request.query.get("n", "")
        png = hass.data.get(DOMAIN, {}).get(f"_img_{name}")
        if not png:
            return web.Response(status=404)
        return web.Response(body=png, content_type="image/png",
                            headers={"Cache-Control": "no-store"})


def ensure_registered(hass) -> None:
    """Register the view once (safe to call repeatedly from the config flow)."""
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("_img_view"):
        hass.http.register_view(ImageView())
        data["_img_view"] = True


def set_image(hass, name: str, png: bytes) -> None:
    hass.data.setdefault(DOMAIN, {})[f"_img_{name}"] = png
