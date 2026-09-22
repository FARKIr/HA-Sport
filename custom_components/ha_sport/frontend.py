"""Serve the Lovelace card and a caching proxy for team / competition logos."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from aiohttp import web

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import CARD_FILENAME, DOMAIN, FRONTEND_URL_BASE, LOGO_URL, VERSION

_LOGGER = logging.getLogger(__name__)

WWW_DIR = Path(__file__).parent / "www"
LOGO_TTL = 7 * 86400


async def async_register_frontend(hass: HomeAssistant) -> None:
    if hass.data[DOMAIN].get("frontend_registered"):
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL_BASE, str(WWW_DIR), cache_headers=False)]
    )
    # the card is loaded automatically – no manual resource needed
    add_extra_js_url(hass, f"{FRONTEND_URL_BASE}/{CARD_FILENAME}?v={VERSION}")
    hass.http.register_view(LogoView(hass))
    hass.data[DOMAIN]["frontend_registered"] = True


class LogoView(HomeAssistantView):
    """Proxy + disk cache for logos (avoids hotlinking and works offline)."""

    url = LOGO_URL + "/{kind}/{obj_id}"
    name = f"api:{DOMAIN}:logo"
    requires_auth = False  # <img> tags cannot send the bearer token; logos are public

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.cache_dir = Path(hass.config.path(".storage", f"{DOMAIN}_logos"))

    async def get(self, request: web.Request, kind: str, obj_id: str) -> web.StreamResponse:
        if kind not in ("team", "tournament") or not obj_id.isdigit():
            return web.Response(status=404)
        path = self.cache_dir / f"{kind}_{obj_id}.png"

        def _read() -> bytes | None:
            if path.exists() and time.time() - path.stat().st_mtime < LOGO_TTL:
                return path.read_bytes()
            return None

        data = await self.hass.async_add_executor_job(_read)
        ctype = "image/png"
        if data is None:
            from . import loaded_runtimes  # pylint: disable=import-outside-toplevel

            runtimes = loaded_runtimes(self.hass)
            result = await runtimes[0].client.image(kind, int(obj_id)) if runtimes else None
            if result is None:
                # stale cache is better than nothing
                if path.exists():
                    data = await self.hass.async_add_executor_job(path.read_bytes)
                else:
                    return web.Response(status=404)
            else:
                data, ctype = result

                def _write() -> None:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)

                await self.hass.async_add_executor_job(_write)
        return web.Response(
            body=data,
            content_type=ctype.split(";")[0],
            headers={"Cache-Control": "public, max-age=604800"},
        )
