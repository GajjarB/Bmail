"""Excel and CSV export.

A spreadsheet is a fine *export* and a terrible *source of truth*: it cannot
dedupe, cannot hold a state machine and cannot tell you what has gone stale.
SQLite does all three; this module renders the answer into the format the rest
of someone's workflow actually speaks.

Five sheets, because that is what the data is: `Overview` (the KPIs),
`Cases`, `Timeline`, `Deadlines`, `Contacts`.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bmail.errors import ExportError
from bmail.packs import Pack
from bmail.store import repo

CASE_COLUMNS = [
    "id", "state", "org", "role", "opened_at", "last_event_at", "last_inbound_at",
    "days_silent", "stalled", "next_action", "next_action_due", "event_datetime",
    "contact_person", "messages", "closed", "key",
]

TIMELINE_COLUMNS = [
    "case_id", "case", "date", "direction", "category", "subject", "from", "summary", "evidence",
]

DEADLINE_COLUMNS = ["due", "days_left", "case_id", "case", "state", "next_action"]

CONTACT_COLUMNS = ["address", "name", "domain", "sent", "received", "first_seen", "last_seen"]


def _case_rows(conn: sqlite3.Connection, pack: Pack, now: datetime) -> list[list[Any]]:
    rows = []
    for case in repo.list_cases(conn, pack.name):
        rows.append(
            [
                case.id, case.state, case.org or "", case.role or "",
                _iso(case.opened_at), _iso(case.last_event_at), _iso(case.last_inbound_at),
                case.days_silent(now), "yes" if case.stalled else "",
                case.next_action.value if hasattr(case.next_action, "value") else case.next_action,
                case.next_action_due.isoformat() if case.next_action_due else "",
                _iso(case.event_datetime), case.contact_person or "",
                case.message_count, "yes" if case.closed else "", case.key,
            ]
        )
    return rows


def _timeline_rows(conn: sqlite3.Connection, pack: Pack) -> list[list[Any]]:
    rows = []
    for case in repo.list_cases(conn, pack.name):
        if case.id is None:
            continue
        for entry in repo.case_timeline(conn, case.id):
            rows.append(
                [
                    case.id, case.title or case.key, entry["date_utc"], entry["direction"],
                    entry["category"] or "", entry["subject"], entry["from_addr"],
                    entry["summary"] or "", entry["evidence"] or "",
                ]
            )
    return rows


def _deadline_rows(conn: sqlite3.Connection, pack: Pack, now: datetime) -> list[list[Any]]:
    rows = []
    for case in repo.list_cases(conn, pack.name, open_only=True):
        if not case.next_action_due:
            continue
        rows.append(
            [
                case.next_action_due.isoformat(),
                (case.next_action_due - now.date()).days,
                case.id, case.title or case.key, case.state,
                case.next_action.value if hasattr(case.next_action, "value") else case.next_action,
            ]
        )
    rows.sort(key=lambda row: row[0])
    return rows


def _contact_rows(conn: sqlite3.Connection) -> list[list[Any]]:
    return [
        [r["address"], r["display_name"], r["domain"], r["sent_count"], r["received_count"],
         r["first_seen"] or "", r["last_seen"] or ""]
        for r in conn.execute(
            "SELECT * FROM contacts ORDER BY sent_count DESC, received_count DESC LIMIT 2000"
        )
    ]


def overview(conn: sqlite3.Connection, pack: Pack, now: datetime | None = None) -> list[list[Any]]:
    """The KPI block. Pack-declared KPIs are computed where the data supports it."""
    now = now or datetime.now(timezone.utc)
    cases = repo.list_cases(conn, pack.name)
    open_cases = [case for case in cases if not case.closed]
    stats = repo.verdict_stats(conn, pack.name)
    counts = repo.count_messages(conn)

    replied = [
        case for case in cases
        if case.state not in (pack.initial_state, "", "ghosted")
    ]
    by_state: dict[str, int] = {}
    for case in cases:
        by_state[case.state] = by_state.get(case.state, 0) + 1

    rows: list[list[Any]] = [
        ["Generated", now.strftime("%Y-%m-%d %H:%M UTC")],
        ["Pack", pack.name],
        ["Messages ingested", counts["total"]],
        ["Messages triaged", stats["total"]],
        ["Needing action", stats["action_required"]],
        ["Flagged for review", stats["needs_review"]],
        ["Cases", len(cases)],
        ["Open cases", len(open_cases)],
        ["Stalled cases", sum(1 for case in cases if case.stalled)],
        [
            "Response rate",
            f"{len(replied) / len(cases):.0%}" if cases else "n/a",
        ],
        [],
        ["Cases by state", ""],
    ]
    rows += [[state, count] for state, count in sorted(by_state.items(), key=lambda kv: -kv[1])]
    rows += [[], ["Messages by triage stage", ""]]
    rows += [[stage, count] for stage, count in sorted(stats["by_stage"].items())]
    rows += [[], ["Messages by category", ""]]
    rows += [[name, count] for name, count in stats["by_category"].items()]
    return rows


def export_xlsx(
    conn: sqlite3.Connection, pack: Pack, path: str | Path, *, now: datetime | None = None
) -> Path:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover - core dependency, but be explicit
        raise ExportError("Excel export needs openpyxl: pip install openpyxl") from exc

    now = now or datetime.now(timezone.utc)
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2F4858")

    def sheet(title: str, columns: list[str], rows: list[list[Any]], freeze: bool = True) -> None:
        worksheet = workbook.create_sheet(title)
        if columns:
            worksheet.append(columns)
            for cell in worksheet[1]:
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = Alignment(vertical="center")
            if freeze:
                worksheet.freeze_panes = "A2"
                worksheet.auto_filter.ref = (
                    f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 2)}"
                )
        for row in rows:
            worksheet.append(row)
        for index, column in enumerate(columns or [""], start=1):
            longest = max(
                [len(str(column))] + [len(str(row[index - 1])) for row in rows[:200] if len(row) >= index]
                or [10]
            )
            worksheet.column_dimensions[get_column_letter(index)].width = min(max(longest + 2, 10), 60)

    workbook.remove(workbook.active)
    sheet("Overview", [], overview(conn, pack, now), freeze=False)
    sheet("Cases", CASE_COLUMNS, _case_rows(conn, pack, now))
    sheet("Timeline", TIMELINE_COLUMNS, _timeline_rows(conn, pack))
    sheet("Deadlines", DEADLINE_COLUMNS, _deadline_rows(conn, pack, now))
    sheet("Contacts", CONTACT_COLUMNS, _contact_rows(conn))
    workbook.save(path)
    return path


def export_csv(
    conn: sqlite3.Connection, pack: Pack, directory: str | Path, *, now: datetime | None = None
) -> list[Path]:
    now = now or datetime.now(timezone.utc)
    directory = Path(directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, columns, rows in (
        ("cases", CASE_COLUMNS, _case_rows(conn, pack, now)),
        ("timeline", TIMELINE_COLUMNS, _timeline_rows(conn, pack)),
        ("deadlines", DEADLINE_COLUMNS, _deadline_rows(conn, pack, now)),
        ("contacts", CONTACT_COLUMNS, _contact_rows(conn)),
    ):
        target = directory / f"{name}.csv"
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            writer.writerows(rows)
        written.append(target)
    return written


def _iso(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""
