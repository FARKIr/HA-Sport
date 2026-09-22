"""Pure notification decision logic (no Home Assistant dependency)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any

from .const import (
    SPORT_BASKETBALL,
    SPORT_EMOJI,
    STATUS_FINISHED,
    STATUS_LIVE,
    STATUS_NOT_STARTED,
)
from .models import event_title, score_text


@dataclass
class Message:
    key: str
    kind: str
    title: str
    message: str
    event: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)


def format_kickoff(ts: float | None, tz=None) -> str:
    if not ts:
        return "?"
    dt = datetime.fromtimestamp(ts, tz)
    days = ["po", "út", "st", "čt", "pá", "so", "ne"]
    return f"{days[dt.weekday()]} {dt.day}. {dt.month}. {dt:%H:%M}"


def odds_line(event: dict[str, Any]) -> str | None:
    odds = event.get("odds") or {}
    parts = [f"{k}: {odds[k]:.2f}" for k in ("1", "X", "2") if odds.get(k)]
    return "Kurzy " + " | ".join(parts) if parts else None


def streams_line(event: dict[str, Any]) -> str | None:
    streams = event.get("streams") or []
    if not streams:
        return None
    return "📺 " + ", ".join(s["platform"] for s in streams[:3])


def minutes_label(minutes: int) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        d = minutes // 1440
        return "zítra" if d == 1 else f"za {d} dny"
    if minutes >= 60 and minutes % 60 == 0:
        h = minutes // 60
        return f"za {h} h"
    return f"za {minutes} min"


def in_quiet_hours(now: datetime, start: str | None, end: str | None) -> bool:
    if not start or not end:
        return False
    try:
        s = dtime.fromisoformat(start)
        e = dtime.fromisoformat(end)
    except ValueError:
        return False
    cur = now.time()
    if s == e:
        return False
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


def pre_match_message(event: dict[str, Any], minutes: int, tz=None) -> Message:
    emoji = SPORT_EMOJI.get(event["sport"], "🏆")
    lines = [f"{event.get('competition') or ''} · {format_kickoff(event.get('timestamp'), tz)}".strip(" ·")]
    if event.get("venue") or event.get("city"):
        lines.append("📍 " + ", ".join(p for p in (event.get("venue"), event.get("city")) if p))
    for extra in (odds_line(event), streams_line(event)):
        if extra:
            lines.append(extra)
    return Message(
        key=f"{event['id']}:pre:{minutes}",
        kind="pre_match",
        title=f"{emoji} {event_title(event)} – {minutes_label(minutes)}",
        message="\n".join(lines),
        event=event,
        extra={"minutes": minutes},
    )


def diff_messages(
    old: dict[str, Any] | None,
    new: dict[str, Any],
    *,
    notify_start: bool = True,
    notify_score: bool = True,
    notify_periods: bool = True,
    notify_end: bool = True,
) -> list[Message]:
    """Compare two snapshots of the same event and produce messages."""
    if old is None:
        return []
    out: list[Message] = []
    emoji = SPORT_EMOJI.get(new["sport"], "🏆")
    title = event_title(new)
    eid = new["id"]
    score = score_text(new)
    minute = new.get("minute") or new.get("status_text") or ""

    if notify_start and old["status"] == STATUS_NOT_STARTED and new["status"] == STATUS_LIVE:
        lines = [new.get("competition") or ""]
        s = streams_line(new)
        if s:
            lines.append(s)
        out.append(Message(f"{eid}:start", "start", f"{emoji} Začíná: {title}", "\n".join(l for l in lines if l), new))

    if (
        notify_score
        and new["status"] in (STATUS_LIVE, STATUS_FINISHED)
        and new["sport"] != SPORT_BASKETBALL
    ):
        oh, oa = old["home"].get("score") or 0, old["away"].get("score") or 0
        nh, na = new["home"].get("score") or 0, new["away"].get("score") or 0
        if (nh, na) != (oh, oa) and (nh + na) > 0:
            scorer = None
            if nh > oh and na == oa:
                scorer = new["home"]["name"]
            elif na > oa and nh == oh:
                scorer = new["away"]["name"]
            word = "GÓL" if new["sport"] == "football" or new["sport"] == "ice-hockey" else "Skóre"
            if nh + na < oh + oa:
                word = "Změna skóre"
                scorer = None
            head = f"{emoji} {word}! {new['home']['short']} {score} {new['away']['short']}"
            body = f"{scorer} · {minute}" if scorer else minute
            out.append(Message(f"{eid}:score:{nh}-{na}", "score", head, body, new, {"scorer": scorer}))

    if (
        notify_periods
        and new["status"] == STATUS_LIVE
        and old["status"] == STATUS_LIVE
        and old.get("status_code") != new.get("status_code")
        and new.get("status_text")
    ):
        out.append(
            Message(
                f"{eid}:period:{new.get('status_code')}",
                "period",
                f"{emoji} {new['status_text']}: {title}",
                f"Stav {score}",
                new,
            )
        )

    if notify_end and old["status"] != STATUS_FINISHED and new["status"] == STATUS_FINISHED:
        out.append(
            Message(
                f"{eid}:end",
                "end",
                f"{emoji} Konec: {new['home']['short']} {score} {new['away']['short']}",
                " · ".join(p for p in (new.get("status_text"), new.get("competition")) if p),
                new,
            )
        )
    return out


def live_update_message(event: dict[str, Any], slot: int) -> Message:
    emoji = SPORT_EMOJI.get(event["sport"], "🏆")
    return Message(
        f"{event['id']}:live:{slot}",
        "live_update",
        f"{emoji} {event['home']['short']} {score_text(event)} {event['away']['short']}",
        " · ".join(p for p in (event.get("minute") or event.get("status_text"), event.get("competition")) if p),
        event,
    )
