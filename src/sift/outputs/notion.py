"""Optional Notion sync.

The design rule here is *fill the tracker that already exists*. Someone
tracking applications already has a database; creating a second one and asking
them to maintain both is how sync tools become abandonware. So Sift matches on a
title property, updates in place, and creates a page only when nothing matches.

Everything is opt-in: no token, no sync. The database id lives in config, never
in the source, because it identifies a private workspace.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from sift.errors import ExportError
from sift.packs import Pack
from sift.store import repo

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"


@dataclass(slots=True)
class SyncReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class NotionClient:
    def __init__(self, token: str | None = None, *, timeout: float = 30.0) -> None:
        self.token = token or os.environ.get("NOTION_TOKEN", "")
        if not self.token:
            raise ExportError(
                "no Notion token. Set NOTION_TOKEN, or leave the Notion sync switched off."
            )
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict:
        request = urllib.request.Request(
            f"{API}{path}",
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": VERSION,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise ExportError(f"Notion API {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ExportError(f"cannot reach the Notion API: {exc.reason}") from exc

    def database(self, database_id: str) -> dict:
        return self._request("GET", f"/databases/{database_id}")

    def find_page(self, database_id: str, title_property: str, title: str) -> str | None:
        result = self._request(
            "POST",
            f"/databases/{database_id}/query",
            {
                "filter": {"property": title_property, "title": {"equals": title}},
                "page_size": 1,
            },
        )
        pages = result.get("results", [])
        return pages[0]["id"] if pages else None

    def create_page(self, database_id: str, properties: dict[str, Any]) -> str:
        result = self._request(
            "POST", "/pages", {"parent": {"database_id": database_id}, "properties": properties}
        )
        return str(result.get("id", ""))

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        self._request("PATCH", f"/pages/{page_id}", {"properties": properties})


def _properties(case, schema: dict[str, Any], title_property: str) -> dict[str, Any]:
    """Map a case onto whichever properties the target database actually has.

    Nothing is created in the target schema — an unmapped field is skipped
    rather than added, so Sift can never restructure someone's database.
    """
    available = schema.get("properties", {})
    properties: dict[str, Any] = {
        title_property: {"title": [{"text": {"content": (case.title or case.key)[:200]}}]}
    }

    def put(name: str, value: Any) -> None:
        definition = available.get(name)
        if not definition or value in (None, ""):
            return
        kind = definition.get("type")
        if kind == "rich_text":
            properties[name] = {"rich_text": [{"text": {"content": str(value)[:1800]}}]}
        elif kind == "select":
            properties[name] = {"select": {"name": str(value)[:100]}}
        elif kind == "status":
            properties[name] = {"status": {"name": str(value)[:100]}}
        elif kind == "date":
            properties[name] = {"date": {"start": str(value)}}
        elif kind == "number":
            properties[name] = {"number": float(value)}
        elif kind == "checkbox":
            properties[name] = {"checkbox": bool(value)}
        elif kind == "url":
            properties[name] = {"url": str(value)}

    for name in ("Status", "State", "Stage"):
        put(name, case.state)
    for name in ("Company", "Organisation", "Organization", "Org", "University"):
        put(name, case.org)
    for name in ("Role", "Position", "Programme", "Program", "Title"):
        put(name, case.role)
    for name in ("Deadline", "Due", "Next action due"):
        put(name, case.next_action_due.isoformat() if case.next_action_due else None)
    for name in ("Interview", "Event", "Event date"):
        put(name, case.event_datetime.date().isoformat() if case.event_datetime else None)
    for name in ("Applied", "Opened", "Applied on"):
        put(name, case.opened_at.date().isoformat() if case.opened_at else None)
    for name in ("Last update", "Last contact", "Updated"):
        put(name, case.last_event_at.date().isoformat() if case.last_event_at else None)
    for name in ("Contact", "Contact person", "Recruiter"):
        put(name, case.contact_person)
    for name in ("Stalled", "Ghosted"):
        put(name, case.stalled)
    for name in ("Messages", "Message count"):
        put(name, case.message_count)
    return properties


def sync_cases(
    conn: sqlite3.Connection,
    pack: Pack,
    database_id: str,
    *,
    token: str | None = None,
    dry_run: bool = True,
    limit: int | None = None,
) -> SyncReport:
    """Push cases into an existing Notion database. Dry run unless told otherwise."""
    report = SyncReport()
    cases = repo.list_cases(conn, pack.name)
    if limit:
        cases = cases[:limit]
    if dry_run:
        report.skipped = len(cases)
        return report

    client = NotionClient(token)
    schema = client.database(database_id)
    title_property = next(
        (
            name
            for name, definition in schema.get("properties", {}).items()
            if definition.get("type") == "title"
        ),
        None,
    )
    if title_property is None:
        raise ExportError("the target Notion database has no title property")

    for case in cases:
        title = (case.title or case.key)[:200]
        try:
            properties = _properties(case, schema, title_property)
            existing = client.find_page(database_id, title_property, title)
            if existing:
                client.update_page(existing, properties)
                report.updated += 1
            else:
                client.create_page(database_id, properties)
                report.created += 1
        except ExportError as exc:
            report.errors.append(f"{title}: {exc}")
    return report
