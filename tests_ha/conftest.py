"""Fixtures for Home Assistant based tests (pytest-homeassistant-custom-component)."""
import time
from unittest.mock import patch

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

NOW = time.time()
UT = 172


def event(eid, home, away, ts, status="notstarted", hs=None, as_=None, code=0, winner=None, ut=UT):
    return {
        "id": eid,
        "slug": f"t{home}-t{away}",
        "customId": f"c{eid}",
        "startTimestamp": int(ts),
        "status": {"code": code, "type": status, "description": "Not started" if status == "notstarted" else "2nd half"},
        "homeTeam": {"id": home, "name": f"Team {home}", "shortName": f"T{home}"},
        "awayTeam": {"id": away, "name": f"Team {away}", "shortName": f"T{away}"},
        "homeScore": {"current": hs} if hs is not None else {},
        "awayScore": {"current": as_} if as_ is not None else {},
        "winnerCode": winner,
        "tournament": {"name": "Chance Liga", "uniqueTournament": {"id": ut, "name": "Chance Liga"},
                       "category": {"name": "Czech Republic", "alpha2": "CZ", "sport": {"slug": "football"}}},
        "roundInfo": {"round": 10},
        "time": {"currentPeriodStartTimestamp": int(NOW - 600)} if status == "inprogress" else {},
    }


class FakeApi:
    def __init__(self):
        self.live = []
        self.responses = {
            "/sport/football/categories": {"categories": [
                {"id": 11, "name": "Czech Republic", "alpha2": "CZ"},
                {"id": 12, "name": "Slovakia", "alpha2": "SK"},
                {"id": 13, "name": "England", "alpha2": "EN"},
            ]},
            "/category/11/unique-tournaments": {"groups": [{"uniqueTournaments": [
                {"id": UT, "name": "Chance Liga"}, {"id": 999, "name": "MOL Cup"}]}]},
            "/category/12/unique-tournaments": {"groups": [{"uniqueTournaments": [{"id": 211, "name": "Niké liga"}]}]},
            f"/unique-tournament/{UT}/seasons": {"seasons": [{"id": 5000, "name": "Chance Liga 26/27", "year": "26/27"}]},
            f"/unique-tournament/{UT}/season/5000/standings/total": {"standings": [{"name": "Chance Liga", "rows": [
                {"position": 1, "team": {"id": 1, "name": "Team 1"}, "matches": 9, "wins": 7, "draws": 2, "losses": 0,
                 "scoresFor": 20, "scoresAgainst": 5, "points": 23},
                {"position": 2, "team": {"id": 2, "name": "Team 2"}, "matches": 9, "wins": 6, "draws": 2, "losses": 1,
                 "scoresFor": 15, "scoresAgainst": 7, "points": 20},
            ]}]},
            f"/unique-tournament/{UT}/season/5000/cuptrees": {"cupTrees": [{"id": 1, "name": "Play-off", "rounds": [
                {"order": 1, "description": "Final", "blocks": [{"blockId": 1, "order": 1, "finished": False,
                 "participants": [{"team": {"id": 1, "name": "Team 1"}, "order": 1}, {"team": {"id": 2, "name": "Team 2"}, "order": 2}],
                 "events": [101]}]}]}]},
            f"/unique-tournament/{UT}/season/5000/events/next/0": {"events": [
                event(101, 1, 2, NOW + 2 * 86400), event(102, 3, 4, NOW + 3 * 86400)]},
            f"/unique-tournament/{UT}/season/5000/events/last/0": {"events": [
                event(90, 2, 1, NOW - 3 * 86400, "finished", 0, 2, 100, 2)]},
            "/team/1/events/next/0": {"events": [event(101, 1, 2, NOW + 2 * 86400)]},
            "/team/1/events/last/0": {"events": [event(90, 2, 1, NOW - 3 * 86400, "finished", 0, 2, 100, 2)]},
            "/event/101/odds/1/featured": {"featured": {"default": {"marketName": "Full time", "choices": [
                {"name": "1", "fractionalValue": "1/1"}, {"name": "X", "fractionalValue": "5/2"}, {"name": "2", "fractionalValue": "3/1"}]}}},
            "/tv/event/101/country-channels": {"countryChannels": {"CZ": [55]}},
            "/tv/channel/55/schedule": {"channel": {"id": 55, "name": "Oneplay Sport 1"}},
            "/team/1": {"team": {"id": 1, "name": "Team 1", "sport": {"slug": "football"},
                                 "venue": {"city": {"name": "Praha"}, "stadium": {"name": "Letná"}}}},
            "/search/all?q=Kometa&page=0": {"results": [{"type": "team", "entity": {
                "id": 77, "name": "HC Kometa Brno", "sport": {"slug": "ice-hockey"}, "country": {"alpha2": "CZ"}}}]},
        }

    async def get(self, path, ttl=0, allow_404=True):
        if path.startswith("/sport/") and path.endswith("/events/live"):
            return {"events": self.live}
        return self.responses.get(path)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def fake_api():
    api = FakeApi()
    with patch("custom_components.ha_sport.api.SofascoreClient.get", side_effect=api.get):
        yield api
