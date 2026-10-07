"""Connection handling and migrations for the single-file store."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from bmail.errors import StoreError
from bmail.text.clean import TEXT_VERSION, clean_body

SCHEMA_VERSION = 1


def _load_schema() -> str:
    return resources.files("bmail.store").joinpath("schema.sql").read_text(encoding="utf-8")


def _restrict(path: Path) -> None:
    """Keep the store readable only by its owner.

    The data directory is already 0700, which is enough on a normal machine —
    but this file holds the full text of someone's mail, and it outlives the
    directory the moment it is copied, backed up or synced. Defence in depth
    costs one syscall.
    """
    for target in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            if target.exists():
                target.chmod(0o600)
        except OSError:  # pragma: no cover - filesystems that refuse chmod
            pass


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the store, creating and migrating it if needed."""
    path = Path(path).expanduser()
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if read_only:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        else:
            conn = sqlite3.connect(path)
    except sqlite3.Error as exc:  # pragma: no cover - environment dependent
        raise StoreError(f"cannot open store at {path}: {exc}") from exc

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        migrate(conn)
        _restrict(path)
    return conn


def _fts5_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.Error:
        return False


def migrate(conn: sqlite3.Connection) -> None:
    """Bring the schema up to `SCHEMA_VERSION`. Safe to call on every open."""
    if not _fts5_available(conn):
        raise StoreError(
            "this Python's SQLite was built without FTS5; install a build with "
            "FTS5 enabled (most distribution builds have it) or run inside a venv "
            "using a newer Python"
        )
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise StoreError(
            f"store was written by a newer version of Bmail (schema {current} > {SCHEMA_VERSION})"
        )
    with conn:
        conn.executescript(_load_schema())
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
    reclean_bodies(conn)


def reclean_bodies(conn: sqlite3.Connection) -> int:
    """Clean every stored body again when the cleaning rules have changed.

    The text as it came out of the message is kept beside the cleaned one, so
    a better cleaner reaches mail read months ago, not only the next message.
    Runs once per change of `TEXT_VERSION`, and says how many bodies changed.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'text_version'").fetchone()
    if row and row["value"] == str(TEXT_VERSION):
        return 0
    changed = 0
    with conn:
        rows = conn.execute(
            "SELECT b.message_id, b.body_text, b.body_raw_text, m.subject "
            "FROM bodies b JOIN messages m USING(message_id) WHERE b.body_raw_text != ''"
        ).fetchall()
        for item in rows:
            text = clean_body(item["body_raw_text"])
            if text == item["body_text"]:
                continue
            changed += 1
            conn.execute("UPDATE bodies SET body_text = ? WHERE message_id = ?",
                         (text, item["message_id"]))
        if changed:
            # The search index is rebuilt whole. It has no index on message_id,
            # so replacing rows one at a time reads all of it for each: nine
            # minutes for eight thousand messages, with the store locked and
            # the app showing the old text throughout.
            conn.execute("DELETE FROM messages_fts")
            conn.execute(
                "INSERT INTO messages_fts(message_id, subject, body) "
                "SELECT m.message_id, m.subject, b.body_text "
                "FROM messages m JOIN bodies b USING(message_id)"
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('text_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(TEXT_VERSION),),
        )
    return changed


def schema_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.Error:
        return 0
    return int(row["value"]) if row else 0


@contextmanager
def session(path: str | Path, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect(path, read_only=read_only)
    try:
        yield conn
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
