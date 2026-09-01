"""Source connectors and the ingest driver."""

from __future__ import annotations

import sqlite3
from typing import Any

from sift.errors import IngestError
from sift.ingest.base import SyncResult
from sift.ingest.files import FileSource
from sift.ingest.imap import ImapSource
from sift.ingest.raw import parse_message
from sift.store import repo

__all__ = [
    "FileSource",
    "ImapSource",
    "SyncResult",
    "build_source",
    "ingest",
    "parse_message",
]


def build_source(spec: dict[str, Any], own_addresses: set[str] | None = None) -> Any:
    """Instantiate a connector from an account config block."""
    provider = str(spec.get("provider", "")).lower()
    name = str(spec.get("name", provider or "account"))
    if provider in ("file", "mbox", "maildir", "eml"):
        return FileSource(spec["path"], name=name, own_addresses=own_addresses)
    if provider == "imap":
        return ImapSource(
            host=spec["host"],
            username=spec["username"],
            password=str(spec.get("password") or ""),
            name=name,
            port=int(spec.get("port", 993)),
            folder=str(spec.get("folder", "INBOX")),
            own_addresses=own_addresses,
        )
    if provider == "gmail":
        from sift.ingest.gmail import GmailSource

        return GmailSource(
            name=name,
            client_secret_file=spec["client_secret_file"],
            token_file=spec["token_file"],
            allow_modify=bool(spec.get("allow_modify", False)),
            query=str(spec.get("query", "")),
            own_addresses=own_addresses,
        )
    raise IngestError(f"unknown provider '{provider}' for account '{name}'")


def ingest(
    conn: sqlite3.Connection,
    source: Any,
    *,
    limit: int | None = None,
    resume: bool = True,
    batch_size: int = 200,
) -> SyncResult:
    """Pull from one source into the store, committing in batches."""
    repo.upsert_account(conn, source.name, source.provider)
    conn.commit()
    cursor = repo.get_cursor(conn, source.name) if resume else {}

    result = SyncResult()
    batch = []
    for message in source.fetch(cursor, limit=limit):
        batch.append(message)
        if len(batch) >= batch_size:
            new, seen = repo.bulk_upsert(conn, batch)
            result.fetched += seen
            batch.clear()
    if batch:
        new, seen = repo.bulk_upsert(conn, batch)
        result.fetched += seen

    result.cursor = source.cursor()
    if result.cursor:
        repo.set_cursor(conn, source.name, result.cursor)
        conn.commit()
    return result
