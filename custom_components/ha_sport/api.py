"""Async client for the (public, unofficial) Sofascore JSON API."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import quote
from typing import Any

import aiohttp

from .const import DEFAULT_BASE_URL, FALLBACK_BASE_URLS, IMAGE_BASE_URL

_LOGGER = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "cs-CZ,cs;q=0.9,sk;q=0.8,en;q=0.7",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
    "Cache-Control": "no-cache",
}
# With curl_cffi the User-Agent must match the impersonated browser, so only
# the non-identifying headers are sent and curl_cffi adds the rest.
BROWSER_HEADERS = {k: v for k, v in HEADERS.items() if k != "User-Agent"}

try:  # Sofascore rejects clients whose TLS fingerprint is not a real browser
    from curl_cffi.requests import AsyncSession as CurlSession  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - depends on the platform
    CurlSession = None

IMPERSONATE = "chrome"
_curl_session: Any = None


def _get_curl_session() -> Any:
    global _curl_session  # noqa: PLW0603 - one shared session for all entries
    if CurlSession is None:
        return None
    if _curl_session is None:
        _curl_session = CurlSession(impersonate=IMPERSONATE, timeout=20)
    return _curl_session


async def async_close_shared_session() -> None:
    global _curl_session  # noqa: PLW0603
    if _curl_session is not None:
        try:
            await _curl_session.close()
        except Exception:  # noqa: BLE001
            pass
        _curl_session = None


class SportApiError(Exception):
    """Raised when the API cannot be reached."""


class SportApiNotFound(SportApiError):
    """Raised for 404 (no data – e.g. no standings for a cup)."""


class SofascoreClient:
    """Small wrapper with base URL fallback, caching and concurrency limit."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str | None = None,
        concurrency: int = 4,
    ) -> None:
        self._session = session
        primary = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._bases = [primary] + [b for b in FALLBACK_BASE_URLS if b != primary]
        self._sem = asyncio.Semaphore(concurrency)
        self._cache: dict[str, tuple[float, Any]] = {}
        self.request_count = 0
        self.last_error: str | None = None
        # transports in order of preference; a failing one is moved to the end
        self._transports = (["curl", "aiohttp"] if CurlSession is not None else ["aiohttp"])

    async def _fetch(self, url: str, transport: str, json_headers: bool = True) -> tuple[int, bytes, str]:
        """Return (status, body, content type) using the given transport."""
        if transport == "curl":
            resp = await _get_curl_session().get(url, headers=BROWSER_HEADERS if json_headers else None)
            return resp.status_code, resp.content, resp.headers.get("content-type", "")
        async with self._session.get(
            url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            return resp.status, await resp.read(), resp.headers.get("Content-Type", "")

    async def get(self, path: str, ttl: float = 0, allow_404: bool = True) -> Any:
        """GET JSON with optional cache TTL (seconds)."""
        now = time.monotonic()
        if ttl and path in self._cache:
            ts, data = self._cache[path]
            if now - ts < ttl:
                return data
        problems: list[str] = []
        async with self._sem:
            for transport in list(self._transports):
                for idx, base in enumerate(list(self._bases)):
                    url = f"{base}{path}"
                    host = base.split("/")[2]
                    try:
                        self.request_count += 1
                        status, body, _ = await self._fetch(url, transport)
                    except Exception as exc:  # noqa: BLE001 - network errors of both libraries
                        problems.append(f"{host} ({transport}): {type(exc).__name__} {exc}".strip())
                        continue
                    if status == 404:
                        if allow_404:
                            self._cache[path] = (now, None)
                            return None
                        raise SportApiNotFound(path)
                    if status >= 400:
                        problems.append(f"{host} ({transport}): HTTP {status}")
                        continue
                    try:
                        data = json.loads(body)
                    except ValueError:
                        problems.append(f"{host} ({transport}): neplatná odpověď (není JSON)")
                        continue
                    if idx:
                        # promote the working base URL
                        self._bases.insert(0, self._bases.pop(idx))
                    if transport != self._transports[0]:
                        self._transports.remove(transport)
                        self._transports.insert(0, transport)
                    if ttl:
                        self._cache[path] = (now, data)
                    self.last_error = None
                    return data
        message = "; ".join(problems[:4]) or "neznámá chyba"
        self.last_error = message
        # serve stale cache if we have it
        if path in self._cache:
            _LOGGER.debug("Serving stale cache for %s (%s)", path, message)
            return self._cache[path][1]
        if CurlSession is None:
            message += " – knihovna curl_cffi není nainstalovaná"
        raise SportApiError(message)

    async def safe_get(self, path: str, ttl: float = 0) -> Any:
        try:
            return await self.get(path, ttl)
        except SportApiError as exc:
            _LOGGER.debug("Request %s failed: %s", path, exc)
            return None

    # --- Catalogue ---------------------------------------------------------
    async def categories(self, sport: str) -> list[dict[str, Any]]:
        data = await self.get(f"/sport/{sport}/categories", ttl=86400)
        return (data or {}).get("categories") or []

    async def category_tournaments(self, category_id: int) -> list[dict[str, Any]]:
        data = await self.get(f"/category/{category_id}/unique-tournaments", ttl=86400)
        out: list[dict[str, Any]] = []
        for group in (data or {}).get("groups") or []:
            out.extend(group.get("uniqueTournaments") or [])
        return out

    async def seasons(self, ut_id: int) -> list[dict[str, Any]]:
        data = await self.get(f"/unique-tournament/{ut_id}/seasons", ttl=6 * 3600)
        return (data or {}).get("seasons") or []

    async def tournament_info(self, ut_id: int) -> dict[str, Any]:
        data = await self.safe_get(f"/unique-tournament/{ut_id}", ttl=86400)
        return (data or {}).get("uniqueTournament") or {}

    # --- Events ------------------------------------------------------------
    async def season_events(self, ut_id: int, season_id: int, direction: str, page: int = 0) -> tuple[list[dict], bool]:
        data = await self.safe_get(
            f"/unique-tournament/{ut_id}/season/{season_id}/events/{direction}/{page}"
        )
        return (data or {}).get("events") or [], bool((data or {}).get("hasNextPage"))

    async def team_events(self, team_id: int, direction: str, page: int = 0) -> list[dict[str, Any]]:
        data = await self.safe_get(f"/team/{team_id}/events/{direction}/{page}")
        return (data or {}).get("events") or []

    async def live_events(self, sport: str) -> list[dict[str, Any]]:
        data = await self.get(f"/sport/{sport}/events/live")
        return (data or {}).get("events") or []

    async def scheduled_events(self, sport: str, date: str) -> list[dict[str, Any]]:
        data = await self.safe_get(f"/sport/{sport}/scheduled-events/{date}", ttl=300)
        return (data or {}).get("events") or []

    async def event(self, event_id: int) -> dict[str, Any] | None:
        data = await self.safe_get(f"/event/{event_id}")
        return (data or {}).get("event")

    async def incidents(self, event_id: int, ttl: float = 0) -> list[dict[str, Any]]:
        data = await self.safe_get(f"/event/{event_id}/incidents", ttl=ttl)
        return (data or {}).get("incidents") or []

    async def odds(self, event_id: int, ttl: float, provider_id: int = 1) -> dict[str, Any] | None:
        """Odds of one bookmaker (provider). 1 = Sofascore default provider."""
        data = await self.safe_get(f"/event/{event_id}/odds/{provider_id}/featured", ttl=ttl)
        if not data or not data.get("featured"):
            data = await self.safe_get(f"/event/{event_id}/odds/{provider_id}/all", ttl=ttl)
        return data

    async def odds_providers(self, country: str) -> list[dict[str, Any]]:
        """Bookmakers Sofascore shows for a country: [{id, name}]."""
        out: list[dict[str, Any]] = []
        for path in (f"/odds/providers/{country}/web", f"/odds/providers/{country}"):
            data = await self.safe_get(path, ttl=86400)
            items = (data or {}).get("providers") or (data if isinstance(data, list) else [])
            for item in items:
                prov = item.get("provider", item) if isinstance(item, dict) else {}
                pid = prov.get("id")
                if pid and all(o["id"] != pid for o in out):
                    out.append({"id": pid, "name": prov.get("name") or prov.get("slug") or f"#{pid}"})
            if out:
                break
        return out

    # --- Match detail (fetched on demand / for followed matches) -------------
    async def statistics(self, event_id: int) -> dict[str, Any] | None:
        return await self.safe_get(f"/event/{event_id}/statistics", ttl=60)

    async def lineups(self, event_id: int) -> dict[str, Any] | None:
        return await self.safe_get(f"/event/{event_id}/lineups", ttl=120)

    async def h2h(self, event_id: int) -> dict[str, Any] | None:
        return await self.safe_get(f"/event/{event_id}/h2h", ttl=6 * 3600)

    async def votes(self, event_id: int) -> dict[str, Any] | None:
        return await self.safe_get(f"/event/{event_id}/votes", ttl=1800)

    async def pregame_form(self, event_id: int) -> dict[str, Any] | None:
        return await self.safe_get(f"/event/{event_id}/pregame-form", ttl=6 * 3600)

    async def tv_channels(self, event_id: int, countries: list[str], ttl: float) -> list[str]:
        """Return TV channel names broadcasting the event in given countries."""
        data = await self.safe_get(f"/tv/event/{event_id}/country-channels", ttl=ttl)
        mapping = (data or {}).get("countryChannels") or {}
        names: list[str] = []
        for country in countries:
            for ch in mapping.get(country) or []:
                if isinstance(ch, dict):
                    name = ch.get("name")
                else:
                    name = await self.channel_name(ch)
                if name and name not in names:
                    names.append(name)
        return names

    async def channel_name(self, channel_id: int) -> str | None:
        data = await self.safe_get(f"/tv/channel/{channel_id}/schedule", ttl=7 * 86400)
        channel = (data or {}).get("channel") or {}
        return channel.get("name")

    # --- Tables -------------------------------------------------------------
    async def standings(self, ut_id: int, season_id: int) -> dict[str, Any] | None:
        return await self.safe_get(
            f"/unique-tournament/{ut_id}/season/{season_id}/standings/total", ttl=900
        )

    async def cuptrees(self, ut_id: int, season_id: int) -> dict[str, Any] | None:
        return await self.safe_get(
            f"/unique-tournament/{ut_id}/season/{season_id}/cuptrees", ttl=900
        )

    # --- Teams ---------------------------------------------------------------
    async def team(self, team_id: int) -> dict[str, Any] | None:
        data = await self.safe_get(f"/team/{team_id}", ttl=86400)
        return (data or {}).get("team")

    async def search_teams(self, query: str) -> list[dict[str, Any]]:
        data = await self.safe_get(f"/search/all?q={quote(query)}&page=0")
        out = []
        for res in (data or {}).get("results") or []:
            if res.get("type") == "team":
                out.append(res.get("entity") or {})
        if not out:
            data = await self.safe_get(f"/search/teams/{quote(query)}/more")
            out = (data or {}).get("teams") or []
        return out

    async def image(self, kind: str, obj_id: int) -> tuple[bytes, str] | None:
        path = {
            "team": f"/team/{obj_id}/image",
            "tournament": f"/unique-tournament/{obj_id}/image",
        }[kind]
        for transport in self._transports:
            for base in (IMAGE_BASE_URL, *self._bases):
                try:
                    status, body, ctype = await self._fetch(f"{base}{path}", transport, json_headers=False)
                except Exception:  # noqa: BLE001
                    continue
                if status == 200 and body:
                    return body, ctype or "image/png"
        return None
