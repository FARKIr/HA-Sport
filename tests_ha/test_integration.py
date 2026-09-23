from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.ha_sport.const import DOMAIN, EVENT_NOTIFICATION

from .conftest import NOW, UT, event

ENTRY_DATA = {
    "sports": ["football"],
    "countries": ["CZ"],
    "competitions": [{"id": UT, "sport": "football", "country": "CZ", "name": "Chance Liga"}],
    "favorite_teams": [{"id": 1, "name": "Team 1", "sport": "football"}],
    "notify_enabled": True,
    "notify_scope": "favorites",
    "notify_before": ["60", "15"],
    "notify_start": True,
    "notify_score": True,
    "notify_periods": False,
    "notify_end": True,
    "notify_live_interval": 0,
    "notify_targets": [],
    "notify_persistent": True,
}


async def test_config_flow(hass: HomeAssistant, fake_api) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sports": ["football"], "countries": ["CZ", "SK"], "base_url": "https://www.sofascore.com/api/v1"}
    )
    assert result["step_id"] == "competitions"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"competitions": [f"football|CZ|{UT}|Chance Liga"]}
    )
    assert result["step_id"] == "favorites"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"favorite_teams": ["football|1|Team 1"], "search": "Kometa"}
    )
    assert result["step_id"] == "search"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"results": ["ice-hockey|77|HC Kometa Brno"]})
    assert result["step_id"] == "favorites"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"favorite_teams": ["football|1|Team 1", "ice-hockey|77|HC Kometa Brno"]}
    )
    assert result["step_id"] == "notifications"
    with patch("custom_components.ha_sport.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"notify_enabled": True, "notify_scope": "favorites", "notify_before": ["15", "45"], "notify_start": True,
             "notify_score": True, "notify_periods": False, "notify_end": True, "notify_live_interval": 15,
             "notify_targets": [], "notify_persistent": False},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert [t["id"] for t in result["data"]["favorite_teams"]] == [1, 77]
    assert result["data"]["competitions"][0]["id"] == UT
    assert result["data"]["notify_before"] == ["15", "45"]


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=ENTRY_DATA, title="HA Sport")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_entities_ws_services(hass: HomeAssistant, hass_ws_client, fake_api) -> None:
    entry = await _setup(hass)
    coord = entry.runtime_data.coordinator
    assert 101 in coord.events and coord.events[101]["odds"]["1"] == 2.0
    assert coord.events[101]["streams"][0]["platform"] == "Oneplay"

    states = {s.entity_id: s for s in hass.states.async_all()}
    next_sensor = next(s for eid, s in states.items() if eid.startswith("sensor.team_1") and s.attributes.get("event_id") == 101)
    assert next_sensor.attributes["odds_team"] == 2.0
    assert next_sensor.attributes["home_away"] == "doma"
    assert next_sensor.attributes["stream_url"] == "https://www.oneplay.cz/"
    assert next_sensor.attributes["form"] == "W"
    assert any(eid.startswith("calendar.") for eid in states)
    assert any(eid.startswith("switch.") for eid in states)
    pos = next(s for eid, s in states.items() if eid.startswith("sensor.team_1") and s.attributes.get("points") == 23)
    assert pos.state == "1"

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "ha_sport/overview"})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["competitions"][0]["has_bracket"] is True

    await client.send_json({"id": 2, "type": "ha_sport/matches", "status": "upcoming", "query": "team 3"})
    msg = await client.receive_json()
    assert [m["id"] for m in msg["result"]["matches"]] == [102]

    await client.send_json({"id": 3, "type": "ha_sport/competition", "competition_id": str(UT)})
    msg = await client.receive_json()
    block = msg["result"]["bracket"][0]["rounds"][0]["blocks"][0]
    assert block["matches"][0]["id"] == 101 and msg["result"]["standings"][0]["rows"][0]["points"] == 23

    await client.send_json({"id": 4, "type": "ha_sport/team", "team_id": 1})
    msg = await client.receive_json()
    assert msg["result"]["next"]["id"] == 101 and msg["result"]["position"]["position"] == 1

    await client.send_json({"id": 5, "type": "ha_sport/follow", "event_id": 102, "follow": True})
    msg = await client.receive_json()
    assert msg["success"] and 102 in coord.followed

    resp = await hass.services.async_call(DOMAIN, "get_matches", {"city": "praha"}, blocking=True, return_response=True)
    assert 101 in [m["id"] for m in resp["matches"]]
    resp = await hass.services.async_call(DOMAIN, "search_team", {"query": "Kometa"}, blocking=True, return_response=True)
    assert resp["teams"][0]["id"] == 77


async def test_live_notifications(hass: HomeAssistant, fake_api) -> None:
    entry = await _setup(hass)
    coord = entry.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_NOTIFICATION)

    # match 101 goes live with a goal for favorite team 1
    fake_api.live = [event(101, 1, 2, NOW - 600, "inprogress", 0, 0, 6)]
    coord.events[101]["timestamp"] = NOW - 600
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert coord.events[101]["status"] == "inprogress"
    fake_api.live = [event(101, 1, 2, NOW - 600, "inprogress", 1, 0, 6)]
    await coord.async_refresh()
    await hass.async_block_till_done()
    kinds = [e.data["kind"] for e in events]
    assert "start" in kinds and "score" in kinds
    assert coord.update_interval.total_seconds() <= 60
    playing = [s for s in hass.states.async_all("binary_sensor") if s.attributes.get("event_id") == 101 and s.state == "on"]
    assert playing

    # match ends: removed from live feed, detail says finished
    fake_api.live = []
    fake_api.responses["/event/101"] = {"event": event(101, 1, 2, NOW - 6000, "finished", 2, 0, 100, 1)}
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert "end" in [e.data["kind"] for e in events]
    # no duplicates
    assert len([e for e in events if e.data["kind"] == "end"]) == 1


async def test_reminder_catch_up_and_unload(hass: HomeAssistant, fake_api) -> None:
    ev = event(101, 1, 2, NOW + 10 * 60)
    fake_api.responses["/team/1/events/next/0"] = {"events": [ev]}
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    entry = await _setup(hass)
    await hass.async_block_till_done()
    pre = [e.data for e in events if e.data["kind"] == "pre_match"]
    assert pre and pre[0]["minutes"] == 15
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_logo_proxy_registered(hass: HomeAssistant, hass_client, fake_api) -> None:
    await async_setup_component(hass, "http", {})
    await _setup(hass)
    client = await hass_client()
    # invalid kind -> 404 from our view (proves the route exists), card is no longer served
    assert (await client.get("/api/ha_sport/logo/foo/1")).status == 404
    assert (await client.get("/ha_sport_static/ha-sport-card.js")).status == 404


async def test_odds_multiple_bookmakers_and_sensors(hass: HomeAssistant, hass_ws_client, fake_api) -> None:
    entry = await _setup(hass)
    coord = entry.runtime_data.coordinator
    assert [p["name"] for p in coord.providers] == ["Tipsport", "Fortuna"]
    # favorite match: default + Tipsport compared, best odd picked
    odds = coord.events[101]["odds"]
    assert [b["name"] for b in odds["bookmakers"]] == ["Sofascore", "Tipsport"]
    assert odds["best"]["1"] == 2.1 and odds["best_bookmaker"]["1"] == "Tipsport"
    # non favorite match without default odds falls back to the local bookmaker
    assert coord.events[102]["odds"]["1"] == 1.5 and coord.events[102]["odds"]["source"] == "Tipsport"
    # pre-match detail for favorites
    assert coord.events[101]["h2h"]["home_wins"] == 4
    assert coord.events[101]["votes"]["1"] == 60
    assert "lineups" not in coord.events[101]  # lineups are polled only 90 min before kick-off

    win_odds = next(s for s in hass.states.async_all("sensor") if s.attributes.get("best_bookmaker") == "Tipsport")
    assert float(win_odds.state) == 2.0 and win_odds.attributes["best_odds"] == 2.1
    prob = next(s for s in hass.states.async_all("sensor") if s.attributes.get("unit_of_measurement") == "%"
                and s.attributes.get("event_id") == 101)
    assert 40 <= int(prob.state) <= 60

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "ha_sport/event_detail", "event_id": 101})
    msg = await client.receive_json()
    assert msg["success"]
    detail = msg["result"]
    assert detail["lineups"]["home"]["formation"] == "3-4-3"
    assert detail["h2h"]["draws"] == 3


async def test_goal_scorer_red_card_and_odds_alert(hass: HomeAssistant, fake_api) -> None:
    from homeassistant.core import callback  # noqa: F401

    entry = await _setup(hass)
    coord = entry.runtime_data.coordinator
    events = async_capture_events(hass, EVENT_NOTIFICATION)
    odds_events = async_capture_events(hass, "ha_sport_odds_change")

    # odds on my team (home, Team 1) drop from 2.0 to 1.5 -> ha_sport_odds_change
    fake_api.responses["/event/101/odds/1/featured"] = {"featured": {"default": {"choices": [
        {"name": "1", "fractionalValue": "1/2"}, {"name": "X", "fractionalValue": "5/2"}, {"name": "2", "fractionalValue": "4/1"}]}}}
    fake_api.responses.pop("/event/101/odds/55/featured")
    coord._odds_fetched.clear()
    coord._odds_sources.clear()
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert odds_events and odds_events[0].data["outcome"] == "1" and odds_events[0].data["new_odds"] == 1.5

    # goes live, goal with scorer from incidents and a red card
    fake_api.live = [event(101, 1, 2, NOW - 600, "inprogress", 0, 0, 6)]
    coord.events[101]["timestamp"] = NOW - 600
    coord.events[101]["incidents"] = []
    fake_api.responses["/event/101/incidents"] = {"incidents": []}
    await coord.async_refresh()
    await hass.async_block_till_done()
    fake_api.live = [event(101, 1, 2, NOW - 600, "inprogress", 1, 0, 6)]
    fake_api.responses["/event/101/incidents"] = {"incidents": [
        {"incidentType": "goal", "id": 5001, "time": 23, "isHome": True, "homeScore": 1, "awayScore": 0,
         "player": {"shortName": "L. Haraslín"}},
        {"incidentType": "card", "id": 5002, "incidentClass": "red", "time": 30, "isHome": False,
         "player": {"shortName": "T. Holeš"}}]}
    await coord.async_refresh()
    await hass.async_block_till_done()
    goal = [e.data for e in events if e.data["kind"] == "score"]
    assert goal and "L. Haraslín" in goal[-1]["message"]
    assert any(e.data["kind"] == "red_card" and "Holeš" in e.data["title"] for e in events)
