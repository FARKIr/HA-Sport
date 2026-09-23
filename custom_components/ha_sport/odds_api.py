"""Optional second odds source: The Odds API (https://the-odds-api.com, free API key).

Only sports whose title/description mention Czechia or Slovakia are queried, and
the odds are cached for hours to stay inside the free quota (500 requests/month).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

import aiohttp

from .streams import normalize

_LOGGER = logging.getLogger(__name__)

BASE = "https://api.the-odds-api.com/v4"
KEYWORDS = ("czech", "slovak", "czechia", "slovakia")
SPORT_PREFIX = {"football": "soccer", "ice-hockey": "icehockey", "basketball": "basketball"}
# words that say nothing about the club identity
STOP = {"fc", "fk", "sk", "hc", "bk", "mfk", "afk", "tj", "sfc", "as", "ac", "1.", "a.s.", "team", "hk"}


def _tokens(name: str) -> set[str]:
    return {t for t in normalize(name).replace("-", " ").replace(".", " ").split() if t not in STOP and len(t) > 2}


def team_similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if ta and tb and (ta & tb):
        return 1.0
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def match_event(ev: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find The Odds API event for our event (start within 3 h, both teams similar)."""
    best, best_score = None, 0.0
    for cand in candidates:
        try:
            start = datetime.fromisoformat(cand["commence_time"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if abs(start - (ev.get("timestamp") or 0)) > 3 * 3600:
            continue
        home = team_similarity(ev["home"]["name"], cand.get("home_team", ""))
        away = team_similarity(ev["away"]["name"], cand.get("away_team", ""))
        swapped = False
        if min(home, away) < 0.6:
            home2 = team_similarity(ev["home"]["name"], cand.get("away_team", ""))
            away2 = team_similarity(ev["away"]["name"], cand.get("home_team", ""))
            if min(home2, away2) > min(home, away):
                home, away, swapped = home2, away2, True
        score = min(home, away)
        if score >= 0.6 and score > best_score:
            best, best_score = dict(cand, _swapped=swapped), score
    return best


def bookmakers_from_event(cand: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Convert one The Odds API event into [(bookmaker, {'1','X','2'})]."""
    out = []
    home, away = cand.get("home_team"), cand.get("away_team")
    swapped = cand.get("_swapped", False)
    for bm in cand.get("bookmakers") or []:
        for market in bm.get("markets") or []:
            if market.get("key") != "h2h":
                continue
            odds: dict[str, Any] = {}
            for outcome in market.get("outcomes") or []:
                name, price = outcome.get("name"), outcome.get("price")
                if not price:
                    continue
                if name == home:
                    odds["2" if swapped else "1"] = round(float(price), 2)
                elif name == away:
                    odds["1" if swapped else "2"] = round(float(price), 2)
                elif str(name).lower() == "draw":
                    odds["X"] = round(float(price), 2)
            if "1" in odds and "2" in odds:
                out.append((bm.get("title") or bm.get("key"), odds))
    return out


class OddsApiClient:
    def __init__(self, session: aiohttp.ClientSession, api_key: str, interval_hours: float = 6) -> None:
        self._session = session
        self._key = api_key
        self._interval = interval_hours * 3600
        self._sports: list[dict[str, Any]] | None = None
        self._sports_ts = 0.0
        self._odds: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self.remaining: str | None = None
        self.used: str | None = None
        self.last_error: str | None = None

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        params = {"apiKey": self._key, **params}
        async with self._session.get(f"{BASE}{path}", params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            self.remaining = resp.headers.get("x-requests-remaining", self.remaining)
            self.used = resp.headers.get("x-requests-used", self.used)
            if resp.status != 200:
                self.last_error = f"HTTP {resp.status}: {(await resp.text())[:200]}"
                return None
            self.last_error = None
            return await resp.json()

    async def sports(self) -> list[dict[str, Any]]:
        """Czech / Slovak sports keys (the /sports call does not use quota)."""
        if self._sports is None or time.time() - self._sports_ts > 86400:
            data = await self._get("/sports", {}) or []
            self._sports = [
                s for s in data
                if any(k in normalize(f"{s.get('key')} {s.get('title')} {s.get('description')}") for k in KEYWORDS)
            ]
            self._sports_ts = time.time()
        return self._sports or []

    async def odds_for(self, sport: str) -> list[dict[str, Any]]:
        prefix = SPORT_PREFIX.get(sport)
        events: list[dict[str, Any]] = []
        try:
            for s in await self.sports():
                key = s["key"]
                if prefix and not key.startswith(prefix):
                    continue
                cached = self._odds.get(key)
                if cached and time.time() - cached[0] < self._interval:
                    events.extend(cached[1])
                    continue
                data = await self._get(
                    f"/sports/{key}/odds", {"regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
                )
                if data is not None:
                    self._odds[key] = (time.time(), data)
                    events.extend(data)
                elif cached:
                    events.extend(cached[1])
        except (aiohttp.ClientError, TimeoutError) as exc:
            self.last_error = str(exc)
            _LOGGER.debug("The Odds API failed: %s", exc)
        return events
