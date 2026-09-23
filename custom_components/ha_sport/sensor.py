"""Sensors of HA Sport CZ/SK."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SportConfigEntry
from .const import SPORT_ICONS, SPORTS, STATUS_FINISHED, STATUS_LIVE, STATUS_NOT_STARTED
from .entity import SportEntity, TeamEntity, compact_event
from .models import event_title, odds_for_team, score_text

_BIG_ATTRS = frozenset(
    {"upcoming", "results", "standings", "matches", "this_week", "recent", "live", "bracket_rounds",
     "bookmakers", "goals", "streams", "h2h", "fans_vote"}
)


async def async_setup_entry(
    hass: HomeAssistant, entry: SportConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data.coordinator
    entities: list[SensorEntity] = [
        LiveMatchesSensor(coord),
        TodayMatchesSensor(coord),
        WeekFavoritesSensor(coord),
        StatusSensor(coord),
    ]
    for comp in coord.competitions.values():
        entities.append(CompetitionSensor(coord, comp))
    for team in coord.favorites.values():
        entities.append(TeamNextMatchSensor(coord, team))
        entities.append(TeamLastResultSensor(coord, team))
        entities.append(TeamPositionSensor(coord, team))
        entities.append(TeamOddsSensor(coord, team))
        entities.append(TeamWinProbabilitySensor(coord, team))
    async_add_entities(entities)


def _ts(ev: dict[str, Any] | None) -> datetime | None:
    if not ev or not ev.get("start"):
        return None
    return dt_util.parse_datetime(ev["start"])


class CompetitionSensor(SportEntity, SensorEntity):
    """Next match of a competition + table/results in attributes."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = _BIG_ATTRS

    def __init__(self, coordinator, comp: dict[str, Any]) -> None:
        super().__init__(coordinator, f"competition_{comp['id']}")
        self.comp_id = str(comp["id"])
        self._attr_name = comp["name"]
        self._attr_icon = SPORT_ICONS.get(comp["sport"], "mdi:trophy")

    def _events(self) -> list[dict[str, Any]]:
        return [
            e for e in (self.coordinator.data or {}).get("events", [])
            if str(e.get("competition_id")) == self.comp_id
        ]

    @property
    def native_value(self) -> datetime | None:
        live = [e for e in self._events() if e["status"] == STATUS_LIVE]
        if live:
            return _ts(live[0])
        up = [e for e in self._events() if e["status"] == STATUS_NOT_STARTED]
        return _ts(up[0]) if up else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        events = self._events()
        up = [e for e in events if e["status"] == STATUS_NOT_STARTED]
        res = [e for e in events if e["status"] == STATUS_FINISHED][::-1]
        live = [e for e in events if e["status"] == STATUS_LIVE]
        comp = (self.coordinator.data or {}).get("competitions", {}).get(self.comp_id, {})
        standings = []
        for table in comp.get("standings", []):
            standings.append(
                {
                    "name": table["name"],
                    "rows": [
                        {k: r[k] for k in ("position", "team", "team_id", "played", "wins", "draws", "losses", "scores_for", "scores_against", "points")}
                        for r in table["rows"]
                    ],
                }
            )
        leader = standings[0]["rows"][0]["team"] if standings and standings[0]["rows"] else None
        return {
            "competition_id": self.comp_id,
            "sport": SPORTS.get(comp.get("sport"), comp.get("sport")),
            "country": comp.get("country"),
            "season": comp.get("season"),
            "next_match": event_title(up[0]) if up else None,
            "live_count": len(live),
            "leader": leader,
            "has_bracket": comp.get("has_bracket"),
            "upcoming": [compact_event(e) for e in up[:10]],
            "results": [compact_event(e) for e in res[:10]],
            "live": [compact_event(e) for e in live],
            "standings": standings,
            "logo": comp.get("logo"),
        }


class TeamNextMatchSensor(TeamEntity, SensorEntity):
    """Next (or current) match of a favorite team, with odds and streams."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "next_match"
    _unrecorded_attributes = _BIG_ATTRS

    def __init__(self, coordinator, team: dict[str, Any]) -> None:
        super().__init__(coordinator, team, "next")
        self._attr_icon = SPORT_ICONS.get(team.get("sport") or "", "mdi:calendar-clock")

    @property
    def entity_picture(self) -> str | None:
        return f"{self.coordinator.logo_base}/team/{self.team_id}"

    @property
    def native_value(self) -> datetime | None:
        return _ts(self.coordinator.team_summary(self.team_id)["next"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self.coordinator.team_summary(self.team_id)
        nxt = summary["next"]
        attrs: dict[str, Any] = {
            "team_id": self.team_id,
            "team": self.team.get("name"),
            "form": "".join(summary["form"]),
            "this_week": [compact_event(e) for e in summary["this_week"]],
            "has_match_this_week": bool(summary["this_week"]),
        }
        if not nxt:
            return attrs
        is_home = nxt["home"]["id"] == self.team_id
        opp = nxt["away"] if is_home else nxt["home"]
        odds = nxt.get("odds") or {}
        mine, theirs = ("1", "2") if is_home else ("2", "1")
        prob = odds.get("probability") or {}
        streams = nxt.get("streams") or []
        attrs.update(
            {
                "event_id": nxt["id"],
                "match": event_title(nxt),
                "opponent": opp["name"],
                "opponent_id": opp["id"],
                "opponent_logo": opp.get("logo"),
                "home_away": "doma" if is_home else "venku",
                "competition": nxt.get("competition"),
                "round": nxt.get("round"),
                "venue": nxt.get("venue"),
                "city": nxt.get("city"),
                "status": nxt["status"],
                "status_text": nxt.get("status_text"),
                "minute": nxt.get("minute"),
                "score": score_text(nxt) if nxt["status"] != STATUS_NOT_STARTED else None,
                "odds_team": odds.get(mine),
                "odds_draw": odds.get("X"),
                "odds_opponent": odds.get(theirs),
                "odds_1": odds.get("1"),
                "odds_x": odds.get("X"),
                "odds_2": odds.get("2"),
                "win_probability": prob.get(mine),
                "tv": [s["platform"] for s in streams],
                "stream_url": streams[0]["url"] if streams else None,
                "streams": streams,
                "url": nxt.get("url"),
                "followed": nxt["id"] in self.coordinator.followed,
                "odds_best_team": odds_for_team(odds, is_home)["team_best"],
                "odds_best_bookmaker": odds_for_team(odds, is_home)["team_best_bookmaker"],
                "bookmakers": odds.get("bookmakers"),
                "h2h": nxt.get("h2h"),
                "fans_vote": nxt.get("votes"),
                "lineups_confirmed": bool((nxt.get("lineups") or {}).get("confirmed")),
                "goals": [i for i in nxt.get("incidents") or [] if i.get("type") == "goal"],
                "starts_in_minutes": max(0, int((nxt["timestamp"] - time.time()) // 60))
                if nxt.get("timestamp") and nxt["status"] == STATUS_NOT_STARTED
                else None,
            }
        )
        return attrs


class TeamLastResultSensor(TeamEntity, SensorEntity):
    _attr_translation_key = "last_result"
    _attr_icon = "mdi:scoreboard"
    _unrecorded_attributes = _BIG_ATTRS

    def __init__(self, coordinator, team: dict[str, Any]) -> None:
        super().__init__(coordinator, team, "last")

    @property
    def native_value(self) -> str | None:
        last = self.coordinator.team_summary(self.team_id)["last"]
        if not last:
            return None
        return f"{last['home']['short']} {score_text(last)} {last['away']['short']}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        summary = self.coordinator.team_summary(self.team_id)
        last = summary["last"]
        result = summary["form"][0] if summary["form"] else None
        return {
            "result": {"W": "výhra", "D": "remíza", "L": "prohra"}.get(result or ""),
            "form": "".join(summary["form"]),
            "match": compact_event(last),
            "recent": [compact_event(e) for e in summary["recent"]],
        }


class TeamPositionSensor(TeamEntity, SensorEntity):
    _attr_translation_key = "position"
    _attr_icon = "mdi:podium"

    def __init__(self, coordinator, team: dict[str, Any]) -> None:
        super().__init__(coordinator, team, "position")

    @property
    def native_value(self) -> int | None:
        pos = self.coordinator.team_position(self.team_id)
        return pos["position"] if pos else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pos = self.coordinator.team_position(self.team_id) or {}
        return {k: pos.get(k) for k in ("competition", "table", "points", "played", "wins", "draws", "losses", "scores_for", "scores_against", "promotion")}


class LiveMatchesSensor(SportEntity, SensorEntity):
    _attr_translation_key = "live_matches"
    _attr_icon = "mdi:access-point"
    _attr_native_unit_of_measurement = "zápasů"
    _unrecorded_attributes = _BIG_ATTRS

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "live")

    def _live(self) -> list[dict[str, Any]]:
        return [e for e in (self.coordinator.data or {}).get("events", []) if e["status"] == STATUS_LIVE]

    @property
    def native_value(self) -> int:
        return len(self._live())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        live = self._live()
        return {
            "matches": [compact_event(e) for e in live],
            "favorites_live": [event_title(e) for e in live if e.get("favorite")],
        }


class TodayMatchesSensor(SportEntity, SensorEntity):
    _attr_translation_key = "today_matches"
    _attr_icon = "mdi:calendar-today"
    _attr_native_unit_of_measurement = "zápasů"
    _unrecorded_attributes = _BIG_ATTRS

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "today")

    def _today(self) -> list[dict[str, Any]]:
        start = dt_util.start_of_local_day().timestamp()
        end = start + 86400
        return [
            e for e in (self.coordinator.data or {}).get("events", [])
            if start <= (e.get("timestamp") or 0) < end
        ]

    @property
    def native_value(self) -> int:
        return len(self._today())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"matches": [compact_event(e) for e in self._today()]}


class WeekFavoritesSensor(SportEntity, SensorEntity):
    """Matches of favorite teams in the next 7 days."""

    _attr_translation_key = "week_favorites"
    _attr_icon = "mdi:calendar-star"
    _attr_native_unit_of_measurement = "zápasů"
    _unrecorded_attributes = _BIG_ATTRS

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "week_favorites")

    def _week(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            e for e in (self.coordinator.data or {}).get("events", [])
            if e.get("favorite") and e["status"] in (STATUS_NOT_STARTED, STATUS_LIVE)
            and (e.get("timestamp") or 0) <= now + 7 * 86400
        ]

    @property
    def native_value(self) -> int:
        return len(self._week())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"matches": [compact_event(e) for e in self._week()]}


class StatusSensor(SportEntity, SensorEntity):
    """Diagnostics – last update time and API state."""

    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:update"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> datetime | None:
        updated = (self.coordinator.data or {}).get("updated")
        return dt_util.parse_datetime(updated) if updated else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "events": len(data.get("events", [])),
            "competitions": len(data.get("competitions", {})),
            "requests": data.get("requests"),
            "last_error": data.get("error"),
            # name -> id, handy for YAML card config (competition_id / team_id)
            "competition_ids": {c.get("name"): c.get("id") for c in data.get("competitions", {}).values()},
            "team_ids": {t.get("name"): t.get("id") for t in data.get("favorites", [])},
            "odds": data.get("odds_stats"),
            "szlh_error": data.get("szlh_error"),
            "update_interval_s": self.coordinator.update_interval.total_seconds()
            if self.coordinator.update_interval
            else None,
        }


class _TeamOddsBase(TeamEntity, SensorEntity):
    """Common logic for odds based sensors of a favorite team."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"bookmakers", "opening", "change_pct"})

    def _match(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        nxt = self.coordinator.team_summary(self.team_id)["next"]
        if not nxt or not nxt.get("odds"):
            return nxt, {}
        return nxt, odds_for_team(nxt["odds"], nxt["home"]["id"] == self.team_id)

    def _common_attrs(self, nxt: dict[str, Any] | None) -> dict[str, Any]:
        if not nxt:
            return {}
        odds = nxt.get("odds") or {}
        is_home = nxt["home"]["id"] == self.team_id
        mine = "1" if is_home else "2"
        return {
            "event_id": nxt["id"],
            "match": event_title(nxt),
            "start": nxt.get("start"),
            "competition": nxt.get("competition"),
            "status": nxt["status"],
            "home_away": "doma" if is_home else "venku",
            "opponent": (nxt["away"] if is_home else nxt["home"])["name"],
            "source": odds.get("source"),
            "bookmakers": odds.get("bookmakers"),
            "opening": (odds.get("opening") or {}).get(mine),
            "change_pct": (odds.get("change_pct") or {}).get(mine),
        }


class TeamOddsSensor(_TeamOddsBase):
    """Odd on a win of my team in the next match (for automations)."""

    _attr_translation_key = "win_odds"
    _attr_icon = "mdi:cash-multiple"

    def __init__(self, coordinator, team: dict[str, Any]) -> None:
        super().__init__(coordinator, team, "win_odds")

    @property
    def native_value(self) -> float | None:
        return self._match()[1].get("team")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt, view = self._match()
        return {
            **self._common_attrs(nxt),
            "odds_draw": view.get("draw"),
            "odds_opponent": view.get("opponent"),
            "best_odds": view.get("team_best"),
            "best_bookmaker": view.get("team_best_bookmaker"),
            "trend": view.get("team_trend"),
            "is_favorite": bool(view.get("team") and view.get("opponent") and view["team"] < view["opponent"]),
        }


class TeamWinProbabilitySensor(_TeamOddsBase):
    """Win probability derived from bookmakers' odds (margin removed)."""

    _attr_translation_key = "win_probability"
    _attr_icon = "mdi:percent-circle"
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, team: dict[str, Any]) -> None:
        super().__init__(coordinator, team, "win_probability")

    @property
    def native_value(self) -> int | None:
        return self._match()[1].get("probability")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        nxt, view = self._match()
        return {
            **self._common_attrs(nxt),
            "draw_probability": view.get("probability_draw"),
            "opponent_probability": view.get("probability_opponent"),
            "fans_vote": (nxt or {}).get("votes"),
        }
