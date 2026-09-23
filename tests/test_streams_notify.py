from datetime import datetime

from custom_components.ha_sport.notify_logic import (
    diff_messages,
    in_quiet_hours,
    live_update_message,
    odds_change_message,
    minutes_label,
    pre_match_message,
)
from custom_components.ha_sport.streams import default_streams, link_for_channel, normalize, streams_for_event


def test_normalize():
    assert normalize("  ČT  sport ") == "ct sport"


def test_channel_links():
    assert link_for_channel("Oneplay Sport 1")["platform"] == "Oneplay"
    assert link_for_channel("ČT sport")["free"] is True
    assert link_for_channel("JOJ Šport")["platform"] == "JOJ Šport"
    assert "google" in link_for_channel("Unknown TV")["url"]


def test_default_streams():
    assert [s["platform"] for s in default_streams("football", "Chance Liga", "CZ")] == ["Oneplay"]
    assert [s["platform"] for s in default_streams("football", "Niké liga", "SK")] == ["Voyo"]
    assert "JOJ Šport" in [s["platform"] for s in default_streams("ice-hockey", "Tipsport liga", "SK")]
    assert "ČT sport" in [s["platform"] for s in default_streams("ice-hockey", "Tipsport extraliga", "CZ")]
    streams = streams_for_event([], "basketball", "Kooperativa NBL", "CZ")
    assert streams and all(s.get("guessed") for s in streams)
    assert streams_for_event(["ČT sport", "ČT sport"], "football", "x", "CZ")[0]["platform"] == "ČT sport"


def ev(status="notstarted", hs=None, as_=None, code=0, sport="football"):
    return {"id": 7, "sport": sport, "competition": "Chance Liga", "status": status, "status_code": code,
            "status_text": "2. poločas", "minute": "67'", "timestamp": 1_800_000_000, "venue": "Letná", "city": "Praha",
            "odds": {"1": 2.1, "X": 3.4, "2": 3.2}, "streams": [{"platform": "Oneplay", "url": "https://www.oneplay.cz/"}],
            "home": {"id": 1, "name": "AC Sparta Praha", "short": "Sparta", "score": hs},
            "away": {"id": 2, "name": "SK Slavia Praha", "short": "Slavia", "score": as_}}


def test_pre_match():
    msg = pre_match_message(ev(), 15)
    assert msg.key == "7:pre:15"
    assert "za 15 min" in msg.title
    assert "Kurzy 1: 2.10" in msg.message and "Oneplay" in msg.message
    assert minutes_label(60) == "za 1 h" and minutes_label(1440) == "zítra"


def test_diff_start_goal_end():
    kinds = [m.kind for m in diff_messages(ev(), ev("inprogress", 0, 0, 6))]
    assert kinds == ["start"]
    goal = diff_messages(ev("inprogress", 0, 0, 6), ev("inprogress", 1, 0, 6))
    assert goal[0].kind == "score" and "GÓL" in goal[0].title and goal[0].extra["scorer"] == "AC Sparta Praha"
    end = diff_messages(ev("inprogress", 1, 0, 7), ev("finished", 1, 0, 100))
    assert [m.kind for m in end] == ["end"]
    period = diff_messages(ev("inprogress", 1, 0, 6), ev("inprogress", 1, 0, 31), notify_periods=True)
    assert [m.kind for m in period] == ["period"]
    assert diff_messages(None, ev()) == []


def test_basketball_no_score_spam():
    msgs = diff_messages(ev("inprogress", 10, 8, 1, "basketball"), ev("inprogress", 12, 8, 1, "basketball"))
    assert msgs == []


def test_live_update_and_quiet():
    msg = live_update_message(ev("inprogress", 2, 1, 7), 3)
    assert msg.key == "7:live:3" and "2:1" in msg.title
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 30), "22:00:00", "07:00:00")
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0), "22:00:00", "07:00:00")
    assert in_quiet_hours(datetime(2026, 1, 1, 13, 0), "12:00", "14:00")
    assert not in_quiet_hours(datetime(2026, 1, 1, 13, 0), None, None)


def test_odds_change_and_lineups():
    old, new = ev(), ev()
    new["odds"] = {"1": 1.8, "X": 3.4, "2": 3.2}
    msg = odds_change_message(old, new, "1", 5)
    assert msg and msg.kind == "odds_change" and "klesl" in msg.title and msg.extra["change_pct"] == -14.3
    assert odds_change_message(old, new, "1", 20) is None
    new_l = ev()
    new_l["lineups"] = {"confirmed": True, "home": {"formation": "4-4-2", "starters": [{"name": "A"}]},
                        "away": {"starters": [{"name": "B"}]}}
    msgs = diff_messages(ev(), new_l)
    assert [m.kind for m in msgs] == ["lineups"] and "4-4-2" in msgs[0].message
