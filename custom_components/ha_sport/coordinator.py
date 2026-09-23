"""Data update coordinator for HA Sport CZ/SK."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SofascoreClient, SportApiError
from .const import (
    CONF_COMPETITIONS,
    CONF_COUNTRIES,
    CONF_DAYS_AHEAD,
    CONF_DAYS_BACK,
    CONF_FAVORITE_TEAMS,
    CONF_FETCH_ODDS,
    CONF_FETCH_TV,
    CONF_LIVE_SCAN_INTERVAL,
    CONF_ODDS_API_INTERVAL,
    CONF_ODDS_API_KEY,
    CONF_ODDS_BOOKMAKERS,
    CONF_SCAN_INTERVAL,
    CONF_SPORTS,
    DEFAULT_DAYS_AHEAD,
    DEFAULT_DAYS_BACK,
    DEFAULT_LIVE_SCAN_INTERVAL,
    DEFAULT_ODDS_API_INTERVAL,
    DEFAULT_ODDS_BOOKMAKERS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HEAVY_REFRESH_MINUTES,
    LOGO_URL,
    ODDS_CACHE_MINUTES,
    PRE_LIVE_WINDOW_MINUTES,
    STATUS_FINISHED,
    STATUS_LIVE,
    STATUS_NOT_STARTED,
    STORAGE_KEY,
    STORAGE_VERSION,
    TEAM_CACHE_HOURS,
    TV_CACHE_MINUTES,
)
from .models import (
    filter_events,
    merge_bookmakers,
    parse_h2h,
    parse_incidents,
    parse_lineups,
    parse_statistics,
    parse_votes,
    normalize_bracket,
    normalize_event,
    normalize_standings,
    normalize_team,
    parse_odds,
    team_form,
)
from .odds_api import OddsApiClient, bookmakers_from_event, match_event
from .streams import streams_for_event

_LOGGER = logging.getLogger(__name__)

MAX_ODDS_PER_CYCLE = 40
MAX_TV_PER_CYCLE = 25
MAX_TEAM_DETAILS_PER_CYCLE = 20
# enrichment kept when an event is re-read from a list endpoint
KEEP_KEYS = ("odds", "tv", "streams", "incidents", "lineups", "h2h", "votes")


def entry_option(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Read an option falling back to entry data."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


class SportCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches competitions, matches, odds, tables and brackets."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: SofascoreClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=entry_option(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
            config_entry=entry,
        )
        self.entry = entry
        self.client = client
        self.events: dict[int, dict[str, Any]] = {}
        self.seasons: dict[str, dict[str, Any]] = {}
        self.standings: dict[str, list[dict[str, Any]]] = {}
        self.brackets: dict[str, list[dict[str, Any]]] = {}
        self.teams: dict[int, dict[str, Any]] = {}
        self._team_fetched: dict[int, float] = {}
        self._odds_fetched: dict[int, float] = {}
        self._tv_fetched: dict[int, float] = {}
        self._heavy_ts = 0.0
        self._full_ts = 0.0
        self.followed: set[int] = set()
        self.muted: set[int] = set()
        self.sent_keys: dict[str, float] = {}
        self._store: Store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self.logo_base = LOGO_URL
        # odds from several places
        self.providers: list[dict[str, Any]] = []
        self._odds_sources: dict[int, list[tuple[str, dict[str, Any]]]] = {}
        self._external_odds: dict[int, list[tuple[str, dict[str, Any]]]] = {}
        self.odds_opening: dict[int, dict[str, float]] = {}
        self._detail_fetched: dict[str, float] = {}
        key = entry_option(entry, CONF_ODDS_API_KEY)
        self.odds_api: OddsApiClient | None = (
            OddsApiClient(
                client._session,  # noqa: SLF001 - plain aiohttp session of HA
                key.strip(),
                float(entry_option(entry, CONF_ODDS_API_INTERVAL, DEFAULT_ODDS_API_INTERVAL)),
            )
            if key and key.strip()
            else None
        )

    # --- configuration helpers ---------------------------------------------
    def opt(self, key: str, default: Any = None) -> Any:
        return entry_option(self.entry, key, default)

    @property
    def competitions(self) -> dict[str, dict[str, Any]]:
        return {str(c["id"]): c for c in self.opt(CONF_COMPETITIONS, [])}

    @property
    def favorites(self) -> dict[int, dict[str, Any]]:
        return {int(t["id"]): t for t in self.opt(CONF_FAVORITE_TEAMS, [])}

    @property
    def sports(self) -> list[str]:
        sports = set(self.opt(CONF_SPORTS, []))
        sports.update(c["sport"] for c in self.competitions.values())
        sports.update(t.get("sport") for t in self.favorites.values() if t.get("sport"))
        return sorted(s for s in sports if s)

    @property
    def countries(self) -> list[str]:
        return list(self.opt(CONF_COUNTRIES, ["CZ", "SK"]))

    # --- persistence ----------------------------------------------------------
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.followed = set(int(i) for i in data.get("followed", []))
        self.muted = set(int(i) for i in data.get("muted", []))
        cutoff = time.time() - 3 * 86400
        self.sent_keys = {k: v for k, v in data.get("sent", {}).items() if v > cutoff}
        for team in data.get("teams", []):
            self.teams[int(team["id"])] = team
        self.odds_opening = {int(k): v for k, v in (data.get("odds_opening") or {}).items()}

    def async_schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_store, 10)

    def _data_to_store(self) -> dict[str, Any]:
        cutoff = time.time() - 3 * 86400
        return {
            "followed": sorted(self.followed),
            "muted": sorted(self.muted),
            "sent": {k: v for k, v in self.sent_keys.items() if v > cutoff},
            "teams": list(self.teams.values()),
            "odds_opening": {str(k): v for k, v in self.odds_opening.items() if k in self.events},
        }

    # --- update ---------------------------------------------------------------
    async def _async_update_data(self) -> dict[str, Any]:
        now = time.time()
        scan = self.opt(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL) * 60
        try:
            if now - self._heavy_ts > HEAVY_REFRESH_MINUTES * 60 or not self.seasons:
                await self._refresh_heavy()
                self._heavy_ts = now
            if now - self._full_ts >= scan - 5 or not self.events:
                await self._refresh_events()
                self._full_ts = now
            await self._refresh_live()
            await self._refresh_details()
        except SportApiError as exc:
            if not self.events:
                raise UpdateFailed(f"Sofascore API nedostupné: {exc}") from exc
            _LOGGER.warning("Částečná aktualizace selhala: %s", exc)

        self._prune()
        self._adjust_interval()
        self.async_schedule_save()
        return self._snapshot()

    async def _refresh_heavy(self) -> None:
        """Seasons, standings, brackets – refreshed hourly."""

        async def one(ut_id: str, comp: dict[str, Any]) -> None:
            seasons = await self.client.seasons(int(ut_id))
            if not seasons:
                return
            season = seasons[0]
            self.seasons[ut_id] = season
            standings = normalize_standings(
                await self.client.standings(int(ut_id), season["id"]), self.logo_base
            )
            if standings:
                self.standings[ut_id] = standings
                for table in standings:
                    for row in table["rows"]:
                        tid = row["team_id"]
                        if tid and tid not in self.teams:
                            self.teams[tid] = {
                                "id": tid,
                                "name": row["team"],
                                "short": row["short"],
                                "sport": comp["sport"],
                                "logo": row["logo"],
                                "country": comp.get("country"),
                            }
            bracket = normalize_bracket(
                await self.client.cuptrees(int(ut_id), season["id"]), self.logo_base
            )
            if bracket:
                self.brackets[ut_id] = bracket
            elif ut_id in self.brackets:
                self.brackets.pop(ut_id)

        if not self.providers:
            for country in self.countries:
                for prov in await self.client.odds_providers(country):
                    if prov["id"] != 1 and all(p["id"] != prov["id"] for p in self.providers):
                        self.providers.append(prov)
        results = await asyncio.gather(
            *(one(ut_id, comp) for ut_id, comp in self.competitions.items()),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, Exception)]
        if errors and len(errors) == len(results):
            raise SportApiError(str(errors[0]))

    def _ingest(self, raw_events: list[dict[str, Any]], sport: str | None) -> None:
        for raw in raw_events:
            if not raw.get("id"):
                continue
            ev = normalize_event(raw, sport, self.logo_base)
            old = self.events.get(ev["id"])
            if old:
                # keep enrichment
                for key in KEEP_KEYS:
                    if not ev.get(key) and old.get(key):
                        ev[key] = old[key]
                ev["city"] = ev["city"] or old.get("city")
                ev["venue"] = ev["venue"] or old.get("venue")
            if not ev.get("country"):
                comp = self.competitions.get(str(ev["competition_id"]))
                ev["country"] = comp.get("country") if comp else None
            self.events[ev["id"]] = ev

    async def _refresh_events(self) -> None:
        """Upcoming and past matches of the competitions and favorite teams."""
        tasks = []
        for ut_id, comp in self.competitions.items():
            season = self.seasons.get(ut_id)
            if not season:
                continue
            for direction in ("next", "last"):
                tasks.append((comp["sport"], self.client.season_events(int(ut_id), season["id"], direction)))
        for tid, team in self.favorites.items():
            for direction in ("next", "last"):
                tasks.append((team.get("sport"), self._team_events_wrapped(tid, direction)))
        results = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
        ok = 0
        for (sport, _), res in zip(tasks, results):
            if isinstance(res, Exception):
                _LOGGER.debug("Event fetch failed: %s", res)
                continue
            events = res[0] if isinstance(res, tuple) else res
            self._ingest(events, sport)
            ok += 1
        if tasks and not ok:
            raise SportApiError("Nepodařilo se načíst žádné zápasy")

    async def _team_events_wrapped(self, team_id: int, direction: str) -> list[dict[str, Any]]:
        return await self.client.team_events(team_id, direction)

    def _is_tracked_raw(self, raw: dict[str, Any]) -> bool:
        if raw.get("id") in self.events or raw.get("id") in self.followed:
            return True
        ut = ((raw.get("tournament") or {}).get("uniqueTournament") or {}).get("id")
        if str(ut) in self.competitions:
            return True
        fav = self.favorites
        return (raw.get("homeTeam") or {}).get("id") in fav or (raw.get("awayTeam") or {}).get("id") in fav

    async def _refresh_live(self) -> None:
        """Poll live feeds of our sports; finalize matches that left the feed."""
        now = time.time()
        relevant_sports = set()
        for ev in self.events.values():
            if ev["status"] == STATUS_LIVE or (
                ev["status"] == STATUS_NOT_STARTED
                and ev.get("timestamp")
                and ev["timestamp"] - PRE_LIVE_WINDOW_MINUTES * 60 <= now <= ev["timestamp"] + 4 * 3600
            ):
                relevant_sports.add(ev["sport"])
        if not relevant_sports:
            return
        live_ids: set[int] = set()
        for sport in relevant_sports:
            try:
                raw_events = await self.client.live_events(sport)
            except SportApiError as exc:
                _LOGGER.debug("Live feed %s failed: %s", sport, exc)
                continue
            tracked = [r for r in raw_events if self._is_tracked_raw(r)]
            self._ingest(tracked, sport)
            live_ids.update(r["id"] for r in tracked)
        # matches we believed were live (or should have started) but are not in the feed
        stale = [
            ev["id"]
            for ev in self.events.values()
            if ev["id"] not in live_ids
            and ev["sport"] in relevant_sports
            and (
                ev["status"] == STATUS_LIVE
                or (ev["status"] == STATUS_NOT_STARTED and ev.get("timestamp") and ev["timestamp"] < now - 120)
            )
        ]
        for chunk_start in range(0, len(stale), 8):
            chunk = stale[chunk_start:chunk_start + 8]
            raws = await asyncio.gather(*(self.client.event(eid) for eid in chunk))
            self._ingest([r for r in raws if r], None)

    async def _refresh_details(self) -> None:
        """Odds, TV channels, streaming links and team details (cities)."""
        now = time.time()
        days_ahead = self.opt(CONF_DAYS_AHEAD, DEFAULT_DAYS_AHEAD)
        horizon = now + min(days_ahead, 10) * 86400
        upcoming = sorted(
            (
                e for e in self.events.values()
                if e["status"] in (STATUS_NOT_STARTED, STATUS_LIVE)
                and e.get("timestamp") and e["timestamp"] <= horizon
            ),
            key=lambda e: (not self._is_favorite_event(e), e["timestamp"]),
        )
        tasks = []
        if self.opt(CONF_FETCH_ODDS, True):
            def odds_due(e: dict[str, Any]) -> bool:
                mine = self._is_favorite_event(e) or e["id"] in self.followed
                if e["status"] == STATUS_LIVE:
                    # live odds only for my matches, every 5 minutes
                    return mine and now - self._odds_fetched.get(e["id"], 0) > 300
                ttl = ODDS_CACHE_MINUTES if mine else ODDS_CACHE_MINUTES * 2
                return now - self._odds_fetched.get(e["id"], 0) > ttl * 60

            todo = [e for e in upcoming if odds_due(e)][:MAX_ODDS_PER_CYCLE]
            tasks.extend(self._fetch_odds(e) for e in todo)
        if self.opt(CONF_FETCH_TV, True):
            todo = [
                e for e in upcoming
                if now - self._tv_fetched.get(e["id"], 0) > TV_CACHE_MINUTES * 60
                and e["timestamp"] <= now + 3 * 86400
            ][:MAX_TV_PER_CYCLE]
            tasks.extend(self._fetch_tv(e) for e in todo)
        # team details for city filtering
        missing = [
            tid for tid in set(self.teams) | set(self.favorites)
            if now - self._team_fetched.get(tid, 0) > TEAM_CACHE_HOURS * 3600
            and not (self.teams.get(tid) or {}).get("city_checked")
        ][:MAX_TEAM_DETAILS_PER_CYCLE]
        tasks.extend(self._fetch_team(tid) for tid in missing)
        tasks.append(self._refresh_match_details(upcoming))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.opt(CONF_FETCH_ODDS, True) and self.odds_api:
            await self._refresh_external_odds(upcoming)
        # always make sure there are some stream links
        for ev in self.events.values():
            if not ev.get("streams"):
                ev["streams"] = streams_for_event(ev.get("tv"), ev["sport"], ev.get("competition") or "", ev.get("country"))
            if not ev.get("city"):
                home = self.teams.get(ev["home"]["id"]) or {}
                ev["city"] = home.get("city")
                ev["venue"] = ev.get("venue") or home.get("stadium")

    async def _fetch_odds(self, ev: dict[str, Any], full: bool | None = None) -> None:
        """Odds from the default provider and (for my matches) more bookmakers."""
        eid = ev["id"]
        ttl = 60 if ev["status"] == STATUS_LIVE else ODDS_CACHE_MINUTES * 60
        if full is None:
            full = self._is_favorite_event(ev) or eid in self.followed
        self._odds_fetched[eid] = time.time()
        sources: list[tuple[str, dict[str, Any]]] = []
        main = parse_odds(await self.client.odds(eid, ttl))
        if main:
            sources.append(("Sofascore", main))
        # my matches: compare several bookmakers; others: only when the default has none
        count = int(self.opt(CONF_ODDS_BOOKMAKERS, DEFAULT_ODDS_BOOKMAKERS)) if full else (0 if sources else 3)
        for prov in self.providers[:count]:
            parsed = parse_odds(await self.client.odds(eid, ttl, prov["id"]))
            if parsed:
                sources.append((prov["name"], parsed))
            if not full and sources:
                break
        if sources:
            self._odds_sources[eid] = sources
        self._apply_odds(eid)

    def _apply_odds(self, eid: int) -> None:
        ev = self.events.get(eid)
        if not ev:
            return
        merged = merge_bookmakers(self._odds_sources.get(eid, []) + self._external_odds.get(eid, []))
        if not merged:
            return
        if ev["status"] == STATUS_NOT_STARTED:
            opening = self.odds_opening.setdefault(eid, {k: merged[k] for k in ("1", "X", "2") if merged.get(k)})
        else:
            opening = self.odds_opening.get(eid, {})
        merged["opening"] = opening
        merged["change_pct"] = {
            k: round((merged[k] - opening[k]) / opening[k] * 100, 1)
            for k in ("1", "X", "2") if merged.get(k) and opening.get(k)
        }
        ev["odds"] = merged

    async def _refresh_external_odds(self, upcoming: list[dict[str, Any]]) -> None:
        """Second odds source (The Odds API) for upcoming matches."""
        assert self.odds_api
        by_sport: dict[str, list[dict[str, Any]]] = {}
        for ev in upcoming:
            if ev["status"] == STATUS_NOT_STARTED:
                by_sport.setdefault(ev["sport"], []).append(ev)
        for sport, events in by_sport.items():
            candidates = await self.odds_api.odds_for(sport)
            if not candidates:
                continue
            for ev in events:
                found = match_event(ev, candidates)
                if found:
                    self._external_odds[ev["id"]] = bookmakers_from_event(found)[:8]
                    self._apply_odds(ev["id"])

    def _due(self, key: str, seconds: float) -> bool:
        now = time.time()
        if now - self._detail_fetched.get(key, 0) < seconds:
            return False
        self._detail_fetched[key] = now
        return True

    async def _refresh_match_details(self, upcoming: list[dict[str, Any]]) -> None:
        """Livesport-like detail for my matches: scorers, cards, lineups, H2H, fan tips."""
        now = time.time()
        mine = [
            e for e in self.events.values()
            if self._is_favorite_event(e) or e["id"] in self.followed
        ]
        tasks = []
        for ev in mine:
            eid, ts = ev["id"], ev.get("timestamp") or 0
            if ev["status"] == STATUS_LIVE:
                tasks.append(self._load_incidents(ev))
            elif ev["status"] == STATUS_FINISHED and now - ts < 5 * 3600 and self._due(f"{eid}:inc_final", 1800):
                tasks.append(self._load_incidents(ev))
            elif ev["status"] == STATUS_NOT_STARTED and ts:
                if ts - now < 90 * 60 and not (ev.get("lineups") or {}).get("confirmed") and self._due(f"{eid}:lineups", 240):
                    tasks.append(self._load_lineups(ev))
                if ts - now < 7 * 86400 and self._due(f"{eid}:pre", 6 * 3600):
                    tasks.append(self._load_prematch(ev))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _load_incidents(self, ev: dict[str, Any]) -> None:
        incidents = parse_incidents(await self.client.incidents(ev["id"]))
        if incidents and ev["id"] in self.events:
            self.events[ev["id"]]["incidents"] = incidents

    async def _load_lineups(self, ev: dict[str, Any]) -> None:
        lineups = parse_lineups(await self.client.lineups(ev["id"]))
        if lineups and ev["id"] in self.events:
            self.events[ev["id"]]["lineups"] = lineups

    async def _load_prematch(self, ev: dict[str, Any]) -> None:
        h2h, votes = await asyncio.gather(self.client.h2h(ev["id"]), self.client.votes(ev["id"]))
        if ev["id"] in self.events:
            target = self.events[ev["id"]]
            target["h2h"] = parse_h2h(h2h) or target.get("h2h")
            target["votes"] = parse_votes(votes) or target.get("votes")

    async def async_event_detail(self, event_id: int) -> dict[str, Any]:
        """Everything about one match (used by the card detail and a service)."""
        event_id = int(event_id)
        ev = self.events.get(event_id)
        if ev is None:
            raw = await self.client.event(event_id)
            if not raw:
                return {}
            self._ingest([raw], None)
            ev = self.events[event_id]
        started = ev["status"] in (STATUS_LIVE, STATUS_FINISHED)
        incidents, stats, lineups, h2h, votes = await asyncio.gather(
            self.client.incidents(event_id, ttl=30) if started else asyncio.sleep(0, result=[]),
            self.client.statistics(event_id) if started else asyncio.sleep(0, result=None),
            self.client.lineups(event_id),
            self.client.h2h(event_id),
            self.client.votes(event_id),
        )
        if ev["status"] == STATUS_NOT_STARTED and self.opt(CONF_FETCH_ODDS, True) and (
            time.time() - self._odds_fetched.get(event_id, 0) > 600 or len(self._odds_sources.get(event_id, [])) < 2
        ):
            await self._fetch_odds(ev, full=True)
        ev["incidents"] = parse_incidents(incidents) or ev.get("incidents") or []
        ev["lineups"] = parse_lineups(lineups) or ev.get("lineups")
        ev["h2h"] = parse_h2h(h2h) or ev.get("h2h")
        ev["votes"] = parse_votes(votes) or ev.get("votes")
        return {**ev, "statistics": parse_statistics(stats)}

    async def _fetch_tv(self, ev: dict[str, Any]) -> None:
        countries = list(dict.fromkeys(self.countries + ["CZ", "SK"]))
        channels = await self.client.tv_channels(ev["id"], countries, TV_CACHE_MINUTES * 60)
        self._tv_fetched[ev["id"]] = time.time()
        if ev["id"] in self.events:
            target = self.events[ev["id"]]
            target["tv"] = channels
            target["streams"] = streams_for_event(
                channels, target["sport"], target.get("competition") or "", target.get("country")
            )

    async def _fetch_team(self, team_id: int) -> None:
        self._team_fetched[team_id] = time.time()
        raw = await self.client.team(team_id)
        if not raw:
            return
        info = normalize_team(raw, None, self.logo_base)
        info["city_checked"] = True
        base = self.teams.get(team_id, {})
        base.update({k: v for k, v in info.items() if v is not None})
        self.teams[team_id] = base

    def _prune(self) -> None:
        now = time.time()
        back = self.opt(CONF_DAYS_BACK, DEFAULT_DAYS_BACK) * 86400
        ahead = self.opt(CONF_DAYS_AHEAD, DEFAULT_DAYS_AHEAD) * 86400
        fav = set(self.favorites)
        keep_next: set[int] = set()
        for tid in fav:
            nxt = self._team_next(tid)
            if nxt:
                keep_next.add(nxt["id"])
            last = self._team_last(tid, 10)
            keep_next.update(e["id"] for e in last)
        for eid, ev in list(self.events.items()):
            ts = ev.get("timestamp") or now
            if eid in keep_next or eid in self.followed:
                continue
            if ev["status"] == STATUS_LIVE:
                continue
            if ts < now - back or ts > now + ahead:
                self.events.pop(eid)
        self.followed = {e for e in self.followed if e in self.events}
        for cache in (self._odds_sources, self._external_odds, self._odds_fetched, self.odds_opening):
            for eid in [k for k in cache if k not in self.events]:
                cache.pop(eid, None)

    def _adjust_interval(self) -> None:
        now = time.time()
        live_window = any(
            ev["status"] == STATUS_LIVE
            or (
                ev["status"] == STATUS_NOT_STARTED
                and ev.get("timestamp")
                and ev["timestamp"] - PRE_LIVE_WINDOW_MINUTES * 60 <= now <= ev["timestamp"] + 3 * 3600
            )
            for ev in self.events.values()
        )
        if live_window:
            interval = timedelta(seconds=max(20, self.opt(CONF_LIVE_SCAN_INTERVAL, DEFAULT_LIVE_SCAN_INTERVAL)))
        else:
            interval = timedelta(minutes=self.opt(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
            # wake up in time for the next kick-off window
            upcoming = [
                ev["timestamp"] for ev in self.events.values()
                if ev["status"] == STATUS_NOT_STARTED and ev.get("timestamp") and ev["timestamp"] > now
            ]
            if upcoming:
                until = min(upcoming) - PRE_LIVE_WINDOW_MINUTES * 60 - now
                if 30 < until < interval.total_seconds():
                    interval = timedelta(seconds=until)
        if interval != self.update_interval:
            _LOGGER.debug("Update interval -> %s", interval)
            self.update_interval = interval

    # --- query helpers ----------------------------------------------------------
    def _is_favorite_event(self, ev: dict[str, Any]) -> bool:
        fav = self.favorites
        return ev["home"]["id"] in fav or ev["away"]["id"] in fav

    def sorted_events(self) -> list[dict[str, Any]]:
        return sorted(self.events.values(), key=lambda e: e.get("timestamp") or 0)

    def _team_next(self, team_id: int) -> dict[str, Any] | None:
        live = [
            e for e in self.events.values()
            if e["status"] == STATUS_LIVE and team_id in (e["home"]["id"], e["away"]["id"])
        ]
        if live:
            return live[0]
        now = time.time()
        upcoming = [
            e for e in self.events.values()
            if e["status"] == STATUS_NOT_STARTED
            and team_id in (e["home"]["id"], e["away"]["id"])
            and (e.get("timestamp") or 0) > now - 3 * 3600
        ]
        return min(upcoming, key=lambda e: e["timestamp"]) if upcoming else None

    def _team_last(self, team_id: int, limit: int = 5) -> list[dict[str, Any]]:
        done = [
            e for e in self.events.values()
            if e["status"] == STATUS_FINISHED and team_id in (e["home"]["id"], e["away"]["id"])
        ]
        done.sort(key=lambda e: e.get("timestamp") or 0, reverse=True)
        return done[:limit]

    def team_position(self, team_id: int) -> dict[str, Any] | None:
        for ut_id, tables in self.standings.items():
            for table in tables:
                for row in table["rows"]:
                    if row["team_id"] == team_id:
                        return {
                            "competition_id": ut_id,
                            "competition": self.competitions.get(ut_id, {}).get("name"),
                            "table": table["name"],
                            **row,
                        }
        return None

    def team_summary(self, team_id: int) -> dict[str, Any]:
        team_id = int(team_id)
        info = dict(self.teams.get(team_id) or self.favorites.get(team_id) or {"id": team_id})
        nxt = self._team_next(team_id)
        last = self._team_last(team_id, 5)
        now = time.time()
        week = [
            e for e in self.events.values()
            if team_id in (e["home"]["id"], e["away"]["id"])
            and e["status"] == STATUS_NOT_STARTED
            and now <= (e.get("timestamp") or 0) <= now + 7 * 86400
        ]
        week.sort(key=lambda e: e["timestamp"])
        return {
            "team": info,
            "favorite": team_id in self.favorites,
            "next": nxt,
            "last": last[0] if last else None,
            "recent": last,
            "this_week": week,
            "form": team_form(self.events.values(), team_id),
            "position": self.team_position(team_id),
        }

    def query(self, **filters: Any) -> list[dict[str, Any]]:
        if filters.pop("favorites_only", False):
            filters["favorites"] = list(self.favorites)
        return filter_events(self.sorted_events(), teams=self.teams, **filters)

    def competition_payload(self, ut_id: str) -> dict[str, Any]:
        comp = dict(self.competitions.get(str(ut_id), {"id": ut_id}))
        season = self.seasons.get(str(ut_id)) or {}
        comp.update(
            {
                "season": season.get("name") or season.get("year"),
                "season_id": season.get("id"),
                "standings": self.standings.get(str(ut_id), []),
                "bracket": self.brackets.get(str(ut_id), []),
                "has_standings": bool(self.standings.get(str(ut_id))),
                "has_bracket": bool(self.brackets.get(str(ut_id))),
                "logo": f"{self.logo_base}/tournament/{ut_id}",
            }
        )
        return comp

    def _snapshot(self) -> dict[str, Any]:
        events = self.sorted_events()
        for ev in events:
            ev["favorite"] = self._is_favorite_event(ev)
            ev["followed"] = ev["id"] in self.followed
        return {
            "events": events,
            "live": [e["id"] for e in events if e["status"] == STATUS_LIVE],
            "competitions": {k: self.competition_payload(k) for k in self.competitions},
            "favorites": list(self.favorites.values()),
            "updated": datetime.now(timezone.utc).isoformat(),
            "requests": self.client.request_count,
            "odds_stats": {
                "upcoming": sum(1 for e in events if e["status"] == STATUS_NOT_STARTED),
                "with_odds": sum(1 for e in events if e["status"] == STATUS_NOT_STARTED and e.get("odds")),
                "bookmakers": ["Sofascore"] + [p["name"] for p in self.providers],
                "odds_api": bool(self.odds_api),
                "odds_api_remaining": self.odds_api.remaining if self.odds_api else None,
                "odds_api_error": self.odds_api.last_error if self.odds_api else None,
            },
            "error": self.client.last_error,
        }
