"""iCalendar export, written by hand against RFC 5545.

Hand-rolled rather than pulled from a library because the surface needed here is
one VEVENT shape, and the three things that actually break calendar imports —
line folding at 75 octets, text escaping, and UTC stamps — are each a few lines.

Interview slots become timed events; deadlines become all-day events with an
alarm the day before.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bmail.packs import Pack
from bmail.store import repo

PRODID = "-//Bmail//Local email triage//EN"


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 line folding: 75 octets, continuation lines start with a space."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks: list[str] = []
    current = bytearray()
    for char in line:
        char_bytes = char.encode("utf-8")
        limit = 75 if not chunks else 74
        if len(current) + len(char_bytes) > limit:
            chunks.append(current.decode("utf-8"))
            current = bytearray()
        current += char_bytes
    if current:
        chunks.append(current.decode("utf-8"))
    return "\r\n ".join(chunks)


def _uid(*parts: str) -> str:
    digest = hashlib.blake2b("|".join(parts).encode(), digest_size=12).hexdigest()
    return f"{digest}@bmail.local"


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _event(
    *,
    uid: str,
    summary: str,
    description: str,
    start: datetime | date,
    end: datetime | date | None = None,
    alarm_hours: int | None = None,
    now: datetime,
) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{_stamp(now)}"]
    if isinstance(start, datetime):
        lines.append(f"DTSTART:{_stamp(start)}")
        finish = end if isinstance(end, datetime) else start + timedelta(hours=1)
        lines.append(f"DTEND:{_stamp(finish)}")
    else:
        lines.append(f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}")
        finish = end if isinstance(end, date) else start + timedelta(days=1)
        lines.append(f"DTEND;VALUE=DATE:{finish.strftime('%Y%m%d')}")
    lines.append(f"SUMMARY:{_escape(summary)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    lines.append("TRANSP:OPAQUE")
    if alarm_hours:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"TRIGGER:-PT{alarm_hours}H",
            f"DESCRIPTION:{_escape(summary)}",
            "END:VALARM",
        ]
    lines.append("END:VEVENT")
    return lines


def build_calendar(
    conn: sqlite3.Connection,
    pack: Pack,
    *,
    include_deadlines: bool = True,
    include_events: bool = True,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Bmail · {pack.name}",
    ]
    for case in repo.list_cases(conn, pack.name, open_only=True):
        label = case.title or case.key
        if include_events and case.event_datetime:
            lines += _event(
                uid=_uid(pack.name, str(case.id), "event", case.event_datetime.isoformat()),
                summary=f"{label} — {case.state.replace('_', ' ')}",
                description=f"Case {case.id} · {case.org or ''} · {case.role or ''}".strip(" ·"),
                start=case.event_datetime,
                alarm_hours=2,
                now=now,
            )
        if include_deadlines and case.next_action_due:
            action = (
                case.next_action.value
                if hasattr(case.next_action, "value")
                else str(case.next_action)
            )
            lines += _event(
                uid=_uid(pack.name, str(case.id), "deadline", case.next_action_due.isoformat()),
                summary=f"Deadline: {label}",
                description=f"Next action: {action} · case {case.id}",
                start=case.next_action_due,
                alarm_hours=24,
                now=now,
            )
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def export_ics(
    conn: sqlite3.Connection, pack: Pack, path: str | Path, *, now: datetime | None = None
) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_calendar(conn, pack, now=now), encoding="utf-8", newline="")
    return path
