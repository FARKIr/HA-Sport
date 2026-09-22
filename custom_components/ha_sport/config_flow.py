"""Config & options flow for HA Sport CZ/SK."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TimeSelector,
)

from .api import SofascoreClient, SportApiError
from .const import (
    CONF_BASE_URL,
    CONF_COMPETITIONS,
    CONF_COUNTRIES,
    CONF_DAYS_AHEAD,
    CONF_DAYS_BACK,
    CONF_FAVORITE_TEAMS,
    CONF_FETCH_ODDS,
    CONF_FETCH_TV,
    CONF_LIVE_SCAN_INTERVAL,
    CONF_NOTIFY_BEFORE,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_END,
    CONF_NOTIFY_LIVE_INTERVAL,
    CONF_NOTIFY_PERIODS,
    CONF_NOTIFY_PERSISTENT,
    CONF_NOTIFY_SCOPE,
    CONF_NOTIFY_SCORE,
    CONF_NOTIFY_START,
    CONF_NOTIFY_TARGETS,
    CONF_QUIET_END,
    CONF_QUIET_START,
    CONF_SCAN_INTERVAL,
    CONF_SPORTS,
    COUNTRIES,
    DEFAULT_BASE_URL,
    DEFAULT_DAYS_AHEAD,
    DEFAULT_DAYS_BACK,
    DEFAULT_LIVE_SCAN_INTERVAL,
    DEFAULT_NOTIFY_BEFORE,
    DEFAULT_NOTIFY_LIVE_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    NAME,
    NOTIFY_BEFORE_CHOICES,
    NOTIFY_SCOPE_ALL,
    NOTIFY_SCOPE_FAVORITES,
    NOTIFY_SCOPE_FOLLOWED,
    SPORT_EMOJI,
    SPORTS,
)
from .models import normalize_team
from .streams import normalize

_LOGGER = logging.getLogger(__name__)

# Competitions pre-selected on first setup (matched on normalized name)
RECOMMENDED = (
    "chance liga", "1. liga", "fortuna liga", "nike liga", "niké liga", "mol cup", "slovnaft cup",
    "extraliga", "tipsport liga", "nbl", "sbl",
)
# never interesting for most users – youth/women leagues are still selectable
DEPRIORITIZED = ("u19", "u21", "u20", "u17", "u18", "women", "zeny", "dorost", "junior", "youth")


def _sel(options: list[tuple[str, str]], multiple: bool = True, mode=SelectSelectorMode.LIST, custom=False) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=[SelectOptionDict(value=v, label=l) for v, l in options],
            multiple=multiple,
            mode=mode,
            custom_value=custom,
        )
    )


def _notify_services(hass: HomeAssistant) -> list[tuple[str, str]]:
    services = hass.services.async_services().get("notify", {})
    return sorted(
        ((f"notify.{name}", f"notify.{name}") for name in services if name not in ("send_message", "persistent_notification")),
    )


async def discover_competitions(client: SofascoreClient, sports: list[str], countries: list[str]) -> list[dict[str, Any]]:
    """Find all competitions of the selected sports & countries."""
    found: list[dict[str, Any]] = []
    for sport in sports:
        categories = await client.categories(sport)
        cats = [c for c in categories if (c.get("alpha2") or (c.get("country") or {}).get("alpha2")) in countries]
        results = await asyncio.gather(*(client.category_tournaments(c["id"]) for c in cats), return_exceptions=True)
        for cat, res in zip(cats, results):
            if isinstance(res, Exception):
                continue
            country = cat.get("alpha2") or (cat.get("country") or {}).get("alpha2")
            for idx, ut in enumerate(res):
                found.append(
                    {
                        "id": ut["id"],
                        "name": ut.get("name"),
                        "sport": sport,
                        "country": country,
                        "priority": idx,
                    }
                )
    return found


def competition_options(comps: list[dict[str, Any]]) -> list[tuple[str, str]]:
    def key(c):
        low = normalize(c["name"])
        return (
            list(SPORTS).index(c["sport"]) if c["sport"] in SPORTS else 9,
            c["country"] != "CZ",
            any(d in low for d in DEPRIORITIZED),
            c["priority"],
        )

    return [
        (
            f"{c['sport']}|{c['country']}|{c['id']}|{c['name']}",
            f"{SPORT_EMOJI.get(c['sport'], '')} {COUNTRIES.get(c['country'], c['country'])} · {c['name']}",
        )
        for c in sorted(comps, key=key)
    ]


def recommended_values(options: list[tuple[str, str]]) -> list[str]:
    out = []
    for value, _ in options:
        name = normalize(value.split("|", 3)[3])
        if any(normalize(r) == name or normalize(r) in name for r in RECOMMENDED) and not any(d in name for d in DEPRIORITIZED):
            out.append(value)
    return out


def parse_competitions(values: list[str]) -> list[dict[str, Any]]:
    out = []
    for v in values:
        sport, country, cid, name = v.split("|", 3)
        out.append({"id": int(cid), "sport": sport, "country": country, "name": name})
    return out


def competition_values(comps: list[dict[str, Any]]) -> list[str]:
    return [f"{c['sport']}|{c['country']}|{c['id']}|{c['name']}" for c in comps]


async def teams_of_competitions(client: SofascoreClient, comps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect team list from standings (or recent matches) of the competitions."""

    async def one(comp: dict[str, Any]) -> list[dict[str, Any]]:
        seasons = await client.seasons(comp["id"])
        if not seasons:
            return []
        sid = seasons[0]["id"]
        teams: dict[int, dict[str, Any]] = {}
        standings = await client.standings(comp["id"], sid)
        for table in (standings or {}).get("standings") or []:
            for row in table.get("rows") or []:
                t = row.get("team") or {}
                teams[t["id"]] = {"id": t["id"], "name": t.get("name"), "sport": comp["sport"]}
        if not teams:
            for direction in ("last", "next"):
                events, _ = await client.season_events(comp["id"], sid, direction)
                for ev in events:
                    for side in ("homeTeam", "awayTeam"):
                        t = ev.get(side) or {}
                        if t.get("id"):
                            teams[t["id"]] = {"id": t["id"], "name": t.get("name"), "sport": comp["sport"]}
        for t in teams.values():
            t["competition"] = comp["name"]
        return list(teams.values())

    results = await asyncio.gather(*(one(c) for c in comps), return_exceptions=True)
    merged: dict[int, dict[str, Any]] = {}
    for res in results:
        if isinstance(res, Exception):
            continue
        for t in res:
            merged.setdefault(t["id"], t)
    return sorted(merged.values(), key=lambda t: (list(SPORTS).index(t["sport"]), normalize(t["name"])))


def team_value(t: dict[str, Any]) -> str:
    return f"{t.get('sport') or ''}|{t['id']}|{t.get('name')}"


def team_label(t: dict[str, Any]) -> str:
    extra = t.get("competition") or COUNTRIES.get(t.get("country") or "", t.get("country") or "")
    return f"{SPORT_EMOJI.get(t.get('sport') or '', '')} {t.get('name')}" + (f"  ({extra})" if extra else "")


def parse_teams(values: list[str]) -> list[dict[str, Any]]:
    out = []
    for v in values:
        sport, tid, name = v.split("|", 2)
        out.append({"id": int(tid), "name": name, "sport": sport or None})
    return out


def notification_schema(defaults: dict[str, Any], hass: HomeAssistant) -> vol.Schema:
    before_opts = [
        (v, {"5": "5 min", "10": "10 min", "15": "15 min", "30": "30 min", "60": "1 hodina", "120": "2 hodiny", "180": "3 hodiny", "1440": "1 den"}[v])
        for v in NOTIFY_BEFORE_CHOICES
    ]
    return vol.Schema(
        {
            vol.Required(CONF_NOTIFY_ENABLED, default=defaults.get(CONF_NOTIFY_ENABLED, True)): BooleanSelector(),
            vol.Required(CONF_NOTIFY_SCOPE, default=defaults.get(CONF_NOTIFY_SCOPE, NOTIFY_SCOPE_FAVORITES)): _sel(
                [
                    (NOTIFY_SCOPE_FAVORITES, "Zápasy oblíbených týmů + ručně sledované"),
                    (NOTIFY_SCOPE_FOLLOWED, "Jen ručně sledované zápasy (zvonek v kartě)"),
                    (NOTIFY_SCOPE_ALL, "Všechny zápasy vybraných soutěží"),
                ],
                multiple=False,
            ),
            vol.Optional(CONF_NOTIFY_BEFORE, default=defaults.get(CONF_NOTIFY_BEFORE, DEFAULT_NOTIFY_BEFORE)): _sel(
                before_opts, custom=True, mode=SelectSelectorMode.DROPDOWN
            ),
            vol.Required(CONF_NOTIFY_START, default=defaults.get(CONF_NOTIFY_START, True)): BooleanSelector(),
            vol.Required(CONF_NOTIFY_SCORE, default=defaults.get(CONF_NOTIFY_SCORE, True)): BooleanSelector(),
            vol.Required(CONF_NOTIFY_PERIODS, default=defaults.get(CONF_NOTIFY_PERIODS, False)): BooleanSelector(),
            vol.Required(CONF_NOTIFY_END, default=defaults.get(CONF_NOTIFY_END, True)): BooleanSelector(),
            vol.Required(
                CONF_NOTIFY_LIVE_INTERVAL, default=defaults.get(CONF_NOTIFY_LIVE_INTERVAL, DEFAULT_NOTIFY_LIVE_INTERVAL)
            ): NumberSelector(NumberSelectorConfig(min=0, max=60, step=5, mode=NumberSelectorMode.SLIDER, unit_of_measurement="min")),
            vol.Optional(CONF_NOTIFY_TARGETS, default=defaults.get(CONF_NOTIFY_TARGETS, [])): _sel(
                _notify_services(hass), custom=True, mode=SelectSelectorMode.DROPDOWN
            ),
            vol.Required(CONF_NOTIFY_PERSISTENT, default=defaults.get(CONF_NOTIFY_PERSISTENT, False)): BooleanSelector(),
            vol.Optional(CONF_QUIET_START, description={"suggested_value": defaults.get(CONF_QUIET_START)}): TimeSelector(),
            vol.Optional(CONF_QUIET_END, description={"suggested_value": defaults.get(CONF_QUIET_END)}): TimeSelector(),
        }
    )


def clean_notification_input(user_input: dict[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    data[CONF_NOTIFY_LIVE_INTERVAL] = int(data.get(CONF_NOTIFY_LIVE_INTERVAL) or 0)
    data[CONF_NOTIFY_BEFORE] = [str(int(v)) for v in data.get(CONF_NOTIFY_BEFORE, []) if str(v).strip().isdigit()]
    data.setdefault(CONF_QUIET_START, None)
    data.setdefault(CONF_QUIET_END, None)
    return data


class SportConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup."""

    VERSION = 1

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self._comp_options: list[tuple[str, str]] = []
        self._teams: list[dict[str, Any]] = []
        self._search: list[dict[str, Any]] = []
        self._selected_teams: list[str] = []

    def _client(self) -> SofascoreClient:
        return SofascoreClient(async_get_clientsession(self.hass), self.data.get(CONF_BASE_URL))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self.data.update(user_input)
            if not user_input[CONF_SPORTS]:
                errors[CONF_SPORTS] = "no_sport"
            elif not user_input[CONF_COUNTRIES]:
                errors[CONF_COUNTRIES] = "no_country"
            else:
                try:
                    comps = await discover_competitions(self._client(), user_input[CONF_SPORTS], user_input[CONF_COUNTRIES])
                except SportApiError as exc:
                    _LOGGER.warning("Sofascore nedostupné: %s", exc)
                    errors["base"] = "cannot_connect"
                else:
                    if not comps:
                        errors["base"] = "no_competitions"
                    else:
                        self._comp_options = competition_options(comps)
                        return await self.async_step_competitions()
        schema = vol.Schema(
            {
                vol.Required(CONF_SPORTS, default=self.data.get(CONF_SPORTS, list(SPORTS))): _sel(
                    [(k, f"{SPORT_EMOJI[k]} {v}") for k, v in SPORTS.items()]
                ),
                vol.Required(CONF_COUNTRIES, default=self.data.get(CONF_COUNTRIES, ["CZ", "SK"])): _sel(
                    [("CZ", "🇨🇿 Česko"), ("SK", "🇸🇰 Slovensko")]
                ),
                vol.Optional(CONF_BASE_URL, default=self.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)): TextSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_competitions(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_COMPETITIONS):
                errors[CONF_COMPETITIONS] = "no_competition"
            else:
                self.data[CONF_COMPETITIONS] = parse_competitions(user_input[CONF_COMPETITIONS])
                self._teams = await teams_of_competitions(self._client(), self.data[CONF_COMPETITIONS])
                return await self.async_step_favorites()
        schema = vol.Schema(
            {
                vol.Required(CONF_COMPETITIONS, default=recommended_values(self._comp_options)): _sel(
                    self._comp_options, mode=SelectSelectorMode.DROPDOWN
                )
            }
        )
        return self.async_show_form(step_id="competitions", data_schema=schema, errors=errors)

    async def async_step_favorites(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._selected_teams = list(user_input.get(CONF_FAVORITE_TEAMS, []))
            query = (user_input.get("search") or "").strip()
            if query:
                results = await self._client().search_teams(query)
                self._search = [normalize_team(r) for r in results]
                self._search = [t for t in self._search if t["sport"] in SPORTS]
                return await self.async_step_search()
            self.data[CONF_FAVORITE_TEAMS] = parse_teams(self._selected_teams)
            return await self.async_step_notifications()
        options = [(team_value(t), team_label(t)) for t in self._teams]
        schema = vol.Schema(
            {
                vol.Optional(CONF_FAVORITE_TEAMS, default=self._selected_teams): _sel(options, mode=SelectSelectorMode.DROPDOWN),
                vol.Optional("search"): TextSelector(),
            }
        )
        return self.async_show_form(step_id="favorites", data_schema=schema)

    async def async_step_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for v in user_input.get("results", []):
                if v not in self._selected_teams:
                    self._selected_teams.append(v)
            known = {team_value(t) for t in self._teams}
            for t in self._search:
                if team_value(t) in self._selected_teams and team_value(t) not in known:
                    self._teams.append(t)
            return await self.async_step_favorites()
        if not self._search:
            return self.async_show_form(
                step_id="search",
                data_schema=vol.Schema({}),
                errors={"base": "no_results"},
            )
        schema = vol.Schema(
            {vol.Optional("results", default=[]): _sel([(team_value(t), team_label(t)) for t in self._search])}
        )
        return self.async_show_form(step_id="search", data_schema=schema)

    async def async_step_notifications(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.data.update(clean_notification_input(user_input))
            sports = ", ".join(SPORTS[s] for s in self.data[CONF_SPORTS])
            countries = " + ".join(self.data[CONF_COUNTRIES])
            return self.async_create_entry(title=f"{NAME} ({countries}: {sports})", data=self.data)
        return self.async_show_form(step_id="notifications", data_schema=notification_schema({}, self.hass))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SportOptionsFlow()


class SportOptionsFlow(OptionsFlow):
    """Change competitions, favorites, notifications and general options."""

    def __init__(self) -> None:
        self._comp_options: list[tuple[str, str]] = []
        self._teams: list[dict[str, Any]] = []
        self._search: list[dict[str, Any]] = []
        self._selected: list[str] = []

    def _get(self, key: str, default: Any = None) -> Any:
        if key in self.config_entry.options:
            return self.config_entry.options[key]
        return self.config_entry.data.get(key, default)

    def _client(self) -> SofascoreClient:
        return SofascoreClient(async_get_clientsession(self.hass), self._get(CONF_BASE_URL))

    def _save(self, changes: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **changes})

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["competitions", "favorites", "notifications", "general"],
        )

    async def async_step_competitions(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and CONF_COMPETITIONS in user_input:
            return self._save({CONF_COMPETITIONS: parse_competitions(user_input[CONF_COMPETITIONS])})
        if user_input is not None or not self._comp_options:
            sports = (user_input or {}).get(CONF_SPORTS) or self._get(CONF_SPORTS, list(SPORTS))
            countries = (user_input or {}).get(CONF_COUNTRIES) or self._get(CONF_COUNTRIES, ["CZ", "SK"])
            try:
                comps = await discover_competitions(self._client(), sports, countries)
                self._comp_options = competition_options(comps)
            except SportApiError:
                errors["base"] = "cannot_connect"
        current = competition_values(self._get(CONF_COMPETITIONS, []))
        # keep currently selected even if discovery failed
        known = {v for v, _ in self._comp_options}
        options = self._comp_options + [(v, v.split("|", 3)[3]) for v in current if v not in known]
        schema = vol.Schema(
            {vol.Required(CONF_COMPETITIONS, default=current): _sel(options, mode=SelectSelectorMode.DROPDOWN)}
        )
        return self.async_show_form(step_id="competitions", data_schema=schema, errors=errors)

    async def async_step_favorites(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self._get(CONF_FAVORITE_TEAMS, [])
        if not self._teams:
            self._teams = await teams_of_competitions(self._client(), self._get(CONF_COMPETITIONS, []))
            known = {t["id"] for t in self._teams}
            self._teams.extend(t for t in current if t["id"] not in known)
            self._selected = [team_value(t) for t in current]
        if user_input is not None:
            self._selected = list(user_input.get(CONF_FAVORITE_TEAMS, []))
            query = (user_input.get("search") or "").strip()
            if query:
                self._search = [normalize_team(r) for r in await self._client().search_teams(query)]
                self._search = [t for t in self._search if t["sport"] in SPORTS]
                return await self.async_step_search()
            return self._save({CONF_FAVORITE_TEAMS: parse_teams(self._selected)})
        options = [(team_value(t), team_label(t)) for t in self._teams]
        values = {v for v, _ in options}
        options += [(v, v.split("|", 2)[2]) for v in self._selected if v not in values]
        schema = vol.Schema(
            {
                vol.Optional(CONF_FAVORITE_TEAMS, default=self._selected): _sel(options, mode=SelectSelectorMode.DROPDOWN),
                vol.Optional("search"): TextSelector(),
            }
        )
        return self.async_show_form(step_id="favorites", data_schema=schema)

    async def async_step_search(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            for v in user_input.get("results", []):
                if v not in self._selected:
                    self._selected.append(v)
            known = {team_value(t) for t in self._teams}
            self._teams.extend(t for t in self._search if team_value(t) in self._selected and team_value(t) not in known)
            return await self.async_step_favorites()
        if not self._search:
            return self.async_show_form(step_id="search", data_schema=vol.Schema({}), errors={"base": "no_results"})
        schema = vol.Schema(
            {vol.Optional("results", default=[]): _sel([(team_value(t), team_label(t)) for t in self._search])}
        )
        return self.async_show_form(step_id="search", data_schema=schema)

    async def async_step_notifications(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(clean_notification_input(user_input))
        defaults = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="notifications", data_schema=notification_schema(defaults, self.hass))

    async def async_step_general(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = dict(user_input)
            for key in (CONF_SCAN_INTERVAL, CONF_LIVE_SCAN_INTERVAL, CONF_DAYS_AHEAD, CONF_DAYS_BACK):
                data[key] = int(data[key])
            return self._save(data)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=self._get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)): NumberSelector(
                    NumberSelectorConfig(min=5, max=240, step=5, mode=NumberSelectorMode.BOX, unit_of_measurement="min")
                ),
                vol.Required(
                    CONF_LIVE_SCAN_INTERVAL, default=self._get(CONF_LIVE_SCAN_INTERVAL, DEFAULT_LIVE_SCAN_INTERVAL)
                ): NumberSelector(NumberSelectorConfig(min=20, max=600, step=10, mode=NumberSelectorMode.BOX, unit_of_measurement="s")),
                vol.Required(CONF_DAYS_AHEAD, default=self._get(CONF_DAYS_AHEAD, DEFAULT_DAYS_AHEAD)): NumberSelector(
                    NumberSelectorConfig(min=1, max=60, mode=NumberSelectorMode.BOX, unit_of_measurement="dní")
                ),
                vol.Required(CONF_DAYS_BACK, default=self._get(CONF_DAYS_BACK, DEFAULT_DAYS_BACK)): NumberSelector(
                    NumberSelectorConfig(min=1, max=60, mode=NumberSelectorMode.BOX, unit_of_measurement="dní")
                ),
                vol.Required(CONF_FETCH_ODDS, default=self._get(CONF_FETCH_ODDS, True)): BooleanSelector(),
                vol.Required(CONF_FETCH_TV, default=self._get(CONF_FETCH_TV, True)): BooleanSelector(),
                vol.Required(CONF_BASE_URL, default=self._get(CONF_BASE_URL, DEFAULT_BASE_URL)): TextSelector(),
            }
        )
        return self.async_show_form(step_id="general", data_schema=schema)
