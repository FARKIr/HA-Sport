"""Async client for the (public, unofficial) Sofascore JSON API."""
from __future__ import annotations

import asyncio
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
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "cs-CZ,cs;q=0.9,sk;q=0.8,en;q=0.7",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
    "Cache-Control": "no-cache",
}


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

    async def get(self, path: str, ttl: float = 0, allow_404: bool = True) -> Any:
        """GET JSON with optional cache TTL (seconds)."""
        now = time.monotonic()
        if ttl and path in self._cache:
            ts, data = self._cache[path]
            if now - ts < ttl:
                return data
        last_exc: Exception | None = None
        async with self._sem:
            for idx, base in enumerate(list(self._bases)):
                url = f"{base}{path}"
                try:
                    self.request_count += 1
                    async with self._session.get(
                        url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)
                    ) as resp:
                        if resp.status == 404:
                            if allow_404:
                                self._cache[path] = (now, None)
                                return None
                            raise SportApiNotFound(path)
                        if resp.status in (403, 429, 503):
                            last_exc = SportApiError(f"HTTP {resp.status} for {url}")
                            continue
                        resp.raise_for_status()
                        data = await resp.json(content_type=None)
                        if idx:
                            # promote the working base URL
                            self._bases.insert(0, self._bases.pop(idx))
                        if ttl:
                            self._cache[path] = (now, data)
                        self.last_error = None
                        return data
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                    last_exc = exc
                    continue
        self.last_error = str(last_exc)
        # serve stale cache if we have it
        if path in self._cache:
            _LOGGER.debug("Serving stale cache for %s (%s)", path, last_exc)
            return self._cache[path][1]
        raise SportApiError(str(last_exc))

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

    async def incidents(self, event_id: int) -> list[dict[str, Any]]:
        data = await self.safe_get(f"/event/{event_id}/incidents")
        return (data or {}).get("incidents") or []

    async def odds(self, event_id: int, ttl: float) -> dict[str, Any] | None:
        data = await self.safe_get(f"/event/{event_id}/odds/1/featured", ttl=ttl)
        if not data or not data.get("featured"):
            data = await self.safe_get(f"/event/{event_id}/odds/1/all", ttl=ttl)
        return data

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
        for base in (IMAGE_BASE_URL, *self._bases):
            try:
                async with self._session.get(
                    f"{base}{path}", headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status != 200:
                        continue
                    return await resp.read(), resp.headers.get("Content-Type", "image/png")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
        return None
