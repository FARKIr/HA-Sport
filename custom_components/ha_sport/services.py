"""Services of HA Sport CZ/SK."""
from __future__ import annotations

import time
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .const import (
    ATTR_EVENT_ID,
    ATTR_QUERY,
    ATTR_TEAM_ID,
    CONF_FAVORITE_TEAMS,
    DOMAIN,
    SPORTS,
)
from .models import normalize_team
from .streams import normalize

SERVICE_REFRESH = "refresh"
SERVICE_FOLLOW = "follow_match"
SERVICE_UNFOLLOW = "unfollow_match"
SERVICE_MUTE = "mute_match"
SERVICE_GET_MATCHES = "get_matches"
SERVICE_GET_TEAM = "get_team"
SERVICE_SEARCH_TEAM = "search_team"
SERVICE_ADD_FAVORITE = "add_favorite"
SERVICE_REMOVE_FAVORITE = "remove_favorite"
SERVICE_TEST_NOTIFICATION = "test_notification"
SERVICE_GET_EVENT = "get_event"

EVENT_SCHEMA = vol.Schema({vol.Required(ATTR_EVENT_ID): vol.Coerce(int)})

GET_MATCHES_SCHEMA = vol.Schema(
    {
        vol.Optional("sport"): vol.In(list(SPORTS)),
        vol.Optional("competition_id"): vol.Coerce(int),
        vol.Optional(ATTR_TEAM_ID): vol.Coerce(int),
        vol.Optional(ATTR_QUERY): cv.string,
        vol.Optional("city"): cv.string,
        vol.Optional("status"): vol.In(["upcoming", "live", "results", "notstarted", "inprogress", "finished"]),
        vol.Optional("favorites_only", default=False): cv.boolean,
        vol.Optional("days_ahead"): vol.Coerce(float),
        vol.Optional("days_back"): vol.Coerce(float),
        vol.Optional("limit", default=50): vol.All(vol.Coerce(int), vol.Range(min=1, max=500)),
    }
)


def _runtimes(hass: HomeAssistant):
    from . import loaded_runtimes  # pylint: disable=import-outside-toplevel

    runtimes = loaded_runtimes(hass)
    if not runtimes:
        raise HomeAssistantError("HA Sport není načten")
    return runtimes


def query_matches(hass: HomeAssistant, params: dict[str, Any]) -> list[dict[str, Any]]:
    params = dict(params)
    now = time.time()
    limit = params.pop("limit", 50)
    days_ahead = params.pop("days_ahead", None)
    days_back = params.pop("days_back", None)
    if days_ahead is not None:
        params["end_ts"] = now + days_ahead * 86400
    if days_back is not None:
        params["start_ts"] = now - days_back * 86400
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for rt in _runtimes(hass):
        for ev in rt.coordinator.query(**params):
            if ev["id"] not in seen:
                seen.add(ev["id"])
                out.append(ev)
    out.sort(key=lambda e: e.get("timestamp") or 0)
    if params.get("status") in ("results", "finished"):
        out = list(reversed(out))
    return out[:limit]


def async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        return

    async def refresh(call: ServiceCall) -> None:
        for rt in _runtimes(hass):
            rt.coordinator._full_ts = 0  # force full refresh
            rt.coordinator._heavy_ts = 0
            await rt.coordinator.async_request_refresh()

    async def follow(call: ServiceCall) -> None:
        eid = call.data[ATTR_EVENT_ID]
        for rt in _runtimes(hass):
            rt.coordinator.followed.add(eid)
            rt.coordinator.muted.discard(eid)
            if eid not in rt.coordinator.events:
                raw = await rt.client.event(eid)
                if raw:
                    rt.coordinator._ingest([raw], None)
            rt.coordinator.async_schedule_save()
            await rt.coordinator.async_request_refresh()

    async def unfollow(call: ServiceCall) -> None:
        eid = call.data[ATTR_EVENT_ID]
        for rt in _runtimes(hass):
            rt.coordinator.followed.discard(eid)
            rt.coordinator.async_schedule_save()
            await rt.coordinator.async_request_refresh()

    async def mute(call: ServiceCall) -> None:
        eid = call.data[ATTR_EVENT_ID]
        for rt in _runtimes(hass):
            rt.coordinator.followed.discard(eid)
            rt.coordinator.muted.add(eid)
            rt.coordinator.async_schedule_save()
            await rt.coordinator.async_request_refresh()

    async def get_matches(call: ServiceCall) -> ServiceResponse:
        return {"matches": query_matches(hass, dict(call.data))}

    async def get_team(call: ServiceCall) -> ServiceResponse:
        return _runtimes(hass)[0].coordinator.team_summary(call.data[ATTR_TEAM_ID])

    async def get_event(call: ServiceCall) -> ServiceResponse:
        return await _runtimes(hass)[0].coordinator.async_event_detail(call.data[ATTR_EVENT_ID])

    async def search_team(call: ServiceCall) -> ServiceResponse:
        rt = _runtimes(hass)[0]
        return {"teams": await search_teams(rt, call.data[ATTR_QUERY], call.data.get("sport"))}

    async def add_favorite(call: ServiceCall) -> None:
        rt = _runtimes(hass)[0]
        tid = call.data[ATTR_TEAM_ID]
        entry = rt.coordinator.entry
        favs = list(rt.coordinator.opt(CONF_FAVORITE_TEAMS, []))
        if any(int(f["id"]) == tid for f in favs):
            return
        info = rt.coordinator.teams.get(tid)
        if not info:
            raw = await rt.client.team(tid)
            if not raw:
                raise ServiceValidationError(f"Tým {tid} nenalezen")
            info = normalize_team(raw)
        favs.append({"id": tid, "name": info.get("name"), "sport": info.get("sport")})
        hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_FAVORITE_TEAMS: favs})

    async def remove_favorite(call: ServiceCall) -> None:
        tid = call.data[ATTR_TEAM_ID]
        for rt in _runtimes(hass):
            entry = rt.coordinator.entry
            favs = [f for f in rt.coordinator.opt(CONF_FAVORITE_TEAMS, []) if int(f["id"]) != tid]
            hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_FAVORITE_TEAMS: favs})

    async def test_notification(call: ServiceCall) -> None:
        for rt in _runtimes(hass):
            rt.notifier.async_test()

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh)
    hass.services.async_register(DOMAIN, SERVICE_FOLLOW, follow, schema=EVENT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_UNFOLLOW, unfollow, schema=EVENT_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_MUTE, mute, schema=EVENT_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_GET_MATCHES, get_matches, schema=GET_MATCHES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_GET_TEAM, get_team,
        schema=vol.Schema({vol.Required(ATTR_TEAM_ID): vol.Coerce(int)}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEARCH_TEAM, search_team,
        schema=vol.Schema({vol.Required(ATTR_QUERY): cv.string, vol.Optional("sport"): vol.In(list(SPORTS))}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ADD_FAVORITE, add_favorite,
        schema=vol.Schema({vol.Required(ATTR_TEAM_ID): vol.Coerce(int)}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REMOVE_FAVORITE, remove_favorite,
        schema=vol.Schema({vol.Required(ATTR_TEAM_ID): vol.Coerce(int)}),
    )
    hass.services.async_register(DOMAIN, SERVICE_TEST_NOTIFICATION, test_notification)
    hass.services.async_register(
        DOMAIN, SERVICE_GET_EVENT, get_event, schema=EVENT_SCHEMA, supports_response=SupportsResponse.ONLY
    )


async def search_teams(rt, query: str, sport: str | None = None) -> list[dict[str, Any]]:
    """Search local team cache first, then the remote API."""
    q = normalize(query)
    local = [
        t for t in rt.coordinator.teams.values()
        if q in normalize(" ".join(str(t.get(k) or "") for k in ("name", "short", "city", "stadium")))
        and (not sport or t.get("sport") == sport)
    ]
    remote = []
    for raw in await rt.client.search_teams(query):
        info = normalize_team(raw, None, rt.coordinator.logo_base)
        if info["sport"] not in SPORTS:
            continue
        if sport and info["sport"] != sport:
            continue
        remote.append(info)
    seen = set()
    out = []
    for t in local + remote:
        if t["id"] in seen:
            continue
        seen.add(t["id"])
        out.append(t)
    return out[:30]
