"""Source connectors and the ingest driver."""

from __future__ import annotations

import sqlite3
from typing import Any

from bmail.errors import IngestError
from bmail.ingest.base import SyncResult
from bmail.ingest.files import FileSource
from bmail.ingest.imap import ImapSource
from bmail.ingest.raw import parse_message
from bmail.store import repo

__all__ = [
    "FileSource",
    "ImapSource",
    "SyncResult",
    "build_source",
    "ingest",
    "parse_message",
]


def build_source(
    spec: dict[str, Any],
    own_addresses: set[str] | None = None,
    *,
    interactive: bool = True,
) -> Any:
    """Instantiate a connector from an account config block.

    `interactive=False` is for syncs nobody is watching: a connector that would
    need a browser sign-in raises SignInRequired instead of opening one.
    """
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
            use_ssl=spec.get("ssl", True) is not False,
        )
    if provider in ("graph", "outlook", "microsoft", "m365"):
        from bmail.ingest.graph import GraphSource

        return GraphSource(
            name=name,
            client_id=str(spec.get("client_id", "")),
            tenant=str(spec.get("tenant", "common")),
            token_cache_file=spec.get("token_cache_file", ""),
            folder=str(spec.get("folder", "inbox")),
            allow_modify=bool(spec.get("allow_modify", False)),
            page_size=int(spec.get("page_size", 50)),
            auth_mode=str(spec.get("auth_mode", "interactive")),
            interactive=interactive,
            own_addresses=own_addresses,
        )
    if provider == "gmail":
        from bmail.ingest.gmail import GmailSource

        return GmailSource(
            name=name,
            client_secret_file=spec["client_secret_file"],
            token_file=spec["token_file"],
            allow_modify=bool(spec.get("allow_modify", False)),
            query=str(spec.get("query", "")),
            own_addresses=own_addresses,
            interactive=interactive,
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
            result.new += new
            batch.clear()
    if batch:
        new, seen = repo.bulk_upsert(conn, batch)
        result.fetched += seen
        result.new += new

    result.cursor = source.cursor()
    if result.cursor:
        repo.set_cursor(conn, source.name, result.cursor)
        conn.commit()
    return result
