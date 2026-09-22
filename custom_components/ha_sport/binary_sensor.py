"""Binary sensors: a favorite team is playing right now / plays today."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SportConfigEntry
from .const import STATUS_LIVE
from .entity import SportEntity, TeamEntity
from .models import score_text


async def async_setup_entry(
    hass: HomeAssistant, entry: SportConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coord = entry.runtime_data.coordinator
    entities: list[BinarySensorEntity] = [AnyFavoriteLiveSensor(coord)]
    for team in coord.favorites.values():
        entities.append(TeamPlayingSensor(coord, team))
        entities.append(TeamPlaysTodaySensor(coord, team))
    async_add_entities(entities)


class TeamPlayingSensor(TeamEntity, BinarySensorEntity):
    _attr_translation_key = "playing"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:whistle"

    def __init__(self, coordinator, team: dict[str, Any]) -> None:
        super().__init__(coordinator, team, "playing")

    def _live(self) -> dict[str, Any] | None:
        nxt = self.coordinator.team_summary(self.team_id)["next"]
        return nxt if nxt and nxt["status"] == STATUS_LIVE else None

    @property
    def is_on(self) -> bool:
        return self._live() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ev = self._live()
        if not ev:
            return {}
        streams = ev.get("streams") or []
        return {
            "event_id": ev["id"],
            "match": f"{ev['home']['name']} – {ev['away']['name']}",
            "score": score_text(ev),
            "minute": ev.get("minute"),
            "status_text": ev.get("status_text"),
            "stream_url": streams[0]["url"] if streams else None,
        }


class TeamPlaysTodaySensor(TeamEntity, BinarySensorEntity):
    _attr_translation_key = "plays_today"
    _attr_icon = "mdi:calendar-check"

    def __init__(self, coordinator, team: dict[str, Any]) -> None:
        super().__init__(coordinator, team, "today")

    def _today(self) -> dict[str, Any] | None:
        start = dt_util.start_of_local_day().timestamp()
        for ev in (self.coordinator.data or {}).get("events", []):
            if self.team_id in (ev["home"]["id"], ev["away"]["id"]) and start <= (ev.get("timestamp") or 0) < start + 86400:
                return ev
        return None

    @property
    def is_on(self) -> bool:
        return self._today() is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        ev = self._today()
        return {"event_id": ev["id"], "start": ev.get("start"), "match": f"{ev['home']['name']} – {ev['away']['name']}"} if ev else {}


class AnyFavoriteLiveSensor(SportEntity, BinarySensorEntity):
    _attr_translation_key = "favorite_live"
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:television-play"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "favorite_live")

    def _live(self) -> list[dict[str, Any]]:
        return [
            e for e in (self.coordinator.data or {}).get("events", [])
            if e["status"] == STATUS_LIVE and (e.get("favorite") or e.get("followed"))
        ]

    @property
    def is_on(self) -> bool:
        return bool(self._live())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "matches": [
                f"{e['home']['short']} {score_text(e)} {e['away']['short']} ({e.get('minute') or e.get('status_text')})"
                for e in self._live()
            ]
        }
