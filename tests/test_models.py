import time

from custom_components.ha_sport.models import (
    filter_events,
    fraction_to_decimal,
    live_minute,
    normalize_bracket,
    normalize_event,
    normalize_standings,
    parse_odds,
    team_form,
)

RAW = {
    "id": 111,
    "slug": "sparta-praha-slavia-praha",
    "customId": "abc",
    "startTimestamp": 1_800_000_000,
    "status": {"code": 0, "description": "Not started", "type": "notstarted"},
    "homeTeam": {"id": 1, "name": "AC Sparta Praha", "shortName": "Sparta", "nameCode": "SPA"},
    "awayTeam": {"id": 2, "name": "SK Slavia Praha", "shortName": "Slavia", "nameCode": "SLA"},
    "homeScore": {},
    "awayScore": {},
    "tournament": {
        "name": "Chance Liga",
        "uniqueTournament": {"id": 172, "name": "Chance Liga"},
        "category": {"name": "Czech Republic", "alpha2": "CZ", "sport": {"slug": "football"}},
    },
    "roundInfo": {"round": 9},
    "venue": {"stadium": {"name": "epet ARENA"}, "city": {"name": "Praha"}},
}


def test_normalize_event():
    ev = normalize_event(RAW)
    assert ev["id"] == 111
    assert ev["sport"] == "football"
    assert ev["competition_id"] == 172
    assert ev["country"] == "CZ"
    assert ev["round"] == "9. kolo"
    assert ev["status"] == "notstarted"
    assert ev["status_text"] == "Nezačalo"
    assert ev["home"]["short"] == "Sparta"
    assert ev["city"] == "Praha"
    assert ev["url"].endswith("/sparta-praha-slavia-praha/abc#id:111")
    assert ev["start"].startswith("2027-01-15")


def test_live_minute_football():
    now = 1_000_000.0
    raw = {"status": {"code": 7, "type": "inprogress", "description": "2nd half"},
           "time": {"currentPeriodStartTimestamp": now - 20 * 60 - 5}}
    assert live_minute(raw, "football", now) == "66'"
    raw["time"]["currentPeriodStartTimestamp"] = now - 47 * 60
    assert live_minute(raw, "football", now) == "90+3'"
    raw["status"] = {"code": 31, "type": "inprogress", "description": "Halftime"}
    assert live_minute(raw, "football", now) == "Poločas"


def test_live_minute_hockey():
    now = 1_000_000.0
    raw = {"status": {"code": 14, "type": "inprogress", "description": "2nd period"},
           "time": {"currentPeriodStartTimestamp": now - 300}}
    assert live_minute(raw, "ice-hockey", now) == "2. třetina 6'"


def test_odds_featured():
    payload = {"featured": {"default": {"marketName": "Full time", "choices": [
        {"name": "1", "fractionalValue": "11/10", "initialFractionalValue": "6/5", "change": -1},
        {"name": "X", "fractionalValue": "12/5"},
        {"name": "2", "fractionalValue": "9/4", "change": 1},
    ]}}}
    odds = parse_odds(payload)
    assert odds["1"] == 2.1 and odds["X"] == 3.4 and odds["2"] == 3.25
    assert odds["1_trend"] == "down" and odds["2_trend"] == "up"
    assert sum(odds["probability"].values()) in (99, 100, 101)
    assert odds["probability"]["1"] > odds["probability"]["2"]


def test_odds_all_markets_and_empty():
    payload = {"markets": [{"marketName": "Home/Away", "choices": [
        {"name": "1", "fractionalValue": "1/2"}, {"name": "2", "fractionalValue": "3/2"}]}]}
    odds = parse_odds(payload)
    assert odds["1"] == 1.5 and odds["2"] == 2.5 and "X" not in odds
    assert parse_odds(None) is None
    assert parse_odds({"featured": {}}) is None
    assert fraction_to_decimal("bad") is None
    assert fraction_to_decimal("2.35") == 2.35


def test_standings_and_bracket():
    st = normalize_standings({"standings": [{"name": "Chance Liga", "rows": [
        {"position": 1, "team": {"id": 2, "name": "Slavia"}, "matches": 9, "wins": 8, "draws": 1, "losses": 0,
         "scoresFor": 20, "scoresAgainst": 4, "points": 25, "promotion": {"text": "Champions League", "id": 1}}]}]})
    assert st[0]["rows"][0]["points"] == 25 and st[0]["rows"][0]["promotion"] == "Champions League"
    br = normalize_bracket({"cupTrees": [{"id": 5, "name": "Playoff", "rounds": [
        {"order": 2, "description": "Semifinals", "blocks": []},
        {"order": 1, "description": "Quarterfinals", "blocks": [
            {"blockId": 9, "order": 1, "finished": True, "homeTeamScore": "4", "awayTeamScore": "2",
             "participants": [{"team": {"id": 1, "name": "Třinec"}, "winner": True, "order": 1},
                              {"team": {"id": 3, "name": "Pardubice"}, "winner": False, "order": 2}],
             "events": [1, 2]}]}]}]})
    assert [r["name"] for r in br[0]["rounds"]] == ["Čtvrtfinále", "Semifinále"]
    block = br[0]["rounds"][0]["blocks"][0]
    assert block["participants"][0]["winner"] and block["home_score"] == "4"


def _ev(eid, home, away, hs, as_, winner, ts, status="finished", city=None):
    return {"id": eid, "sport": "football", "competition_id": 172, "competition": "Chance Liga", "status": status,
            "timestamp": ts, "winner": winner, "city": city, "venue": None,
            "home": {"id": home, "name": f"Team {home}", "short": f"T{home}", "score": hs},
            "away": {"id": away, "name": f"Team {away}", "short": f"T{away}", "score": as_}}


def test_team_form():
    events = [_ev(1, 1, 2, 2, 0, 1, 100), _ev(2, 3, 1, 1, 1, 3, 200), _ev(3, 4, 1, 3, 0, 1, 300)]
    assert team_form(events, 1) == ["L", "D", "W"]


def test_filter_events_by_name_and_city():
    teams = {5: {"city": "Brno", "stadium": "Městský fotbalový stadion Srbská"}}
    events = [
        _ev(1, 5, 2, None, None, None, time.time() + 3600, "notstarted"),
        _ev(2, 3, 4, None, None, None, time.time() + 7200, "notstarted", city="Ostrava"),
    ]
    assert [e["id"] for e in filter_events(events, city="brno", teams=teams)] == [1]
    assert [e["id"] for e in filter_events(events, query="ostrava")] == [2]
    assert [e["id"] for e in filter_events(events, query="team 5")] == [1]
    assert filter_events(events, favorites=[4])[0]["id"] == 2
    assert filter_events(events, status="results") == []
    assert len(filter_events(events, status="upcoming")) == 2
