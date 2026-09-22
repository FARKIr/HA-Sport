"""Where to watch: TV channels and streaming links for CZ/SK competitions.

Sofascore returns (when available) the TV channels per event.  Channel names are
mapped to known streaming platforms.  When no channel information is available a
competition-level default is used, based on the broadcasting rights of the
2025/26 and 2026/27 seasons.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# keyword (normalized, lowercase, no diacritics) -> (label, url, free)
CHANNEL_LINKS: list[tuple[str, str, str, bool]] = [
    ("oneplay", "Oneplay", "https://www.oneplay.cz/", False),
    ("o2 tv", "Oneplay", "https://www.oneplay.cz/", False),
    ("nova sport", "Oneplay (Nova Sport)", "https://www.oneplay.cz/", False),
    ("ct sport plus", "ČT sport Plus", "https://sport.ceskatelevize.cz/", True),
    ("ct sport", "ČT sport", "https://sport.ceskatelevize.cz/", True),
    ("ceska televize", "ČT sport", "https://sport.ceskatelevize.cz/", True),
    ("ct2", "ČT2", "https://www.ceskatelevize.cz/zive/ct2/", True),
    ("canal+", "Canal+ Sport", "https://www.canalplus.cz/", False),
    ("canal plus", "Canal+ Sport", "https://www.canalplus.cz/", False),
    ("premier sport", "Premier Sport", "https://www.oneplay.cz/", False),
    ("voyo", "Voyo", "https://voyo.markiza.sk/", False),
    ("dajto", "Dajto", "https://voyo.markiza.sk/", True),
    ("markiza", "Markíza", "https://voyo.markiza.sk/", True),
    ("joj sport", "JOJ Šport", "https://www.jojplay.sk/", False),
    ("joj play", "JOJ Play", "https://www.jojplay.sk/", False),
    ("joj", "JOJ", "https://www.jojplay.sk/", True),
    ("stvr", "STVR Šport", "https://www.stvr.sk/televizia/live", True),
    ("rtvs", "STVR Šport", "https://www.stvr.sk/televizia/live", True),
    ("sport 1", "Sport1", "https://www.sport1.sk/", False),
    ("sport1", "Sport1", "https://www.sport1.sk/", False),
    ("arena sport", "Arena Sport", "https://www.arenasport.sk/", False),
    ("tvcom", "TVCOM", "https://www.tvcom.cz/", True),
    ("tipsport", "Tipsport TV", "https://www.tipsport.cz/tv", True),
    ("tipos", "Tipos TV", "https://tv.tipos.sk/", True),
    ("chance", "TV Chance", "https://www.chance.cz/tv", True),
    ("dazn", "DAZN", "https://www.dazn.com/", False),
    ("prima", "Prima", "https://www.iprima.cz/", True),
    ("nova", "Nova", "https://www.oneplay.cz/", True),
]

# Competition name keyword -> default broadcasters (used when the API gives nothing)
COMPETITION_DEFAULTS: list[tuple[str, str, list[str]]] = [
    # (sport, normalized competition keyword, channel keywords)
    ("football", "chance liga", ["oneplay"]),
    ("football", "1. liga", ["oneplay"]),
    ("football", "first league", ["oneplay"]),
    ("football", "chance narodni liga", ["oneplay", "tvcom"]),
    ("football", "fnl", ["oneplay", "tvcom"]),
    ("football", "mol cup", ["ct sport", "oneplay"]),
    ("football", "nike liga", ["voyo", "dajto"]),
    ("football", "super liga", ["voyo", "dajto"]),
    ("football", "slovnaft cup", ["stvr", "voyo"]),
    ("ice-hockey", "extraliga", ["oneplay", "ct sport"]),
    ("ice-hockey", "chance liga", ["oneplay", "tvcom"]),
    ("ice-hockey", "tipsport liga", ["joj sport", "joj play"]),
    ("ice-hockey", "tipos extraliga", ["joj sport", "joj play"]),
    ("basketball", "nbl", ["ct sport", "tvcom", "chance"]),
    ("basketball", "sbl", ["joj sport", "tipos"]),
    ("basketball", "extraliga", ["joj sport", "tipos"]),
]

# Country-specific override: Slovak "extraliga" hockey vs Czech "extraliga" hockey
COUNTRY_SPORT_DEFAULTS: dict[tuple[str, str], list[str]] = {
    ("CZ", "football"): ["oneplay"],
    ("CZ", "ice-hockey"): ["oneplay", "ct sport"],
    ("CZ", "basketball"): ["ct sport", "tvcom"],
    ("SK", "football"): ["voyo"],
    ("SK", "ice-hockey"): ["joj sport", "joj play"],
    ("SK", "basketball"): ["joj sport", "tipos"],
}


def normalize(text: str | None) -> str:
    """Lowercase and strip diacritics for loose matching."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def link_for_channel(channel_name: str) -> dict[str, Any]:
    """Return a streaming link description for a TV channel name."""
    norm = normalize(channel_name)
    for keyword, label, url, free in CHANNEL_LINKS:
        if keyword in norm:
            return {"name": channel_name, "platform": label, "url": url, "free": free}
    query = re.sub(r"\s+", "+", channel_name.strip())
    return {
        "name": channel_name,
        "platform": channel_name,
        "url": f"https://www.google.com/search?q={query}+live+stream",
        "free": None,
    }


def _link_for_keyword(keyword: str) -> dict[str, Any] | None:
    for kw, label, url, free in CHANNEL_LINKS:
        if kw == keyword:
            return {"name": label, "platform": label, "url": url, "free": free}
    return None


def default_streams(sport: str, competition: str, country: str | None) -> list[dict[str, Any]]:
    """Guess the broadcasters of a competition when the API provides none."""
    comp = normalize(competition)
    keywords: list[str] | None = None
    # Slovak hockey "extraliga" must not match Czech defaults
    if country and (country, sport) in COUNTRY_SPORT_DEFAULTS and country == "SK":
        keywords = COUNTRY_SPORT_DEFAULTS[(country, sport)]
    if keywords is None:
        for c_sport, key, kws in COMPETITION_DEFAULTS:
            if c_sport == sport and key in comp:
                keywords = kws
                break
    if keywords is None and country:
        keywords = COUNTRY_SPORT_DEFAULTS.get((country, sport))
    if not keywords:
        return []
    out = []
    for kw in keywords:
        link = _link_for_keyword(kw)
        if link and all(link["platform"] != o["platform"] for o in out):
            out.append(link)
    return out


def streams_for_event(
    channels: list[str] | None, sport: str, competition: str, country: str | None
) -> list[dict[str, Any]]:
    """Build the final list of where-to-watch links for an event."""
    out: list[dict[str, Any]] = []
    for ch in channels or []:
        link = link_for_channel(ch)
        if all(link["platform"] != o["platform"] for o in out):
            out.append(link)
    if not out:
        for link in default_streams(sport, competition, country):
            link = dict(link, guessed=True)
            out.append(link)
    return out
