from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from sift.ingest.raw import parse_message
from sift.models import Message
from sift.packs import load_pack
from sift.store.db import connect


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    connection = connect(tmp_path / "test.sqlite3")
    yield connection
    connection.close()


@pytest.fixture()
def pack():
    return load_pack("job-seeker")


def make_raw(
    *,
    subject: str = "Test",
    body: str = "Hello there, this is a message body long enough to matter.",
    sender: str = "someone@example.com",
    sender_name: str = "Someone",
    to: str = "you@example.com",
    date: str = "Mon, 14 Sep 2026 10:00:00 +0200",
    message_id: str = "<test@example.com>",
    extra: dict[str, str] | None = None,
    html: str | None = None,
) -> bytes:
    headers = [
        f"Message-ID: {message_id}",
        f"Subject: {subject}",
        f"From: {sender_name} <{sender}>",
        f"To: {to}",
        f"Date: {date}",
        "MIME-Version: 1.0",
    ]
    for name, value in (extra or {}).items():
        headers.append(f"{name}: {value}")
    if html is None:
        headers.append("Content-Type: text/plain; charset=utf-8")
        payload = body
    else:
        headers.append("Content-Type: text/html; charset=utf-8")
        payload = html
    return ("\r\n".join(headers) + "\r\n\r\n" + payload).encode("utf-8")


@pytest.fixture()
def message_factory():
    def build(**kwargs) -> Message:
        own = kwargs.pop("own_addresses", {"you@example.com"})
        return parse_message(make_raw(**kwargs), own_addresses=own)

    return build


@pytest.fixture()
def now() -> datetime:
    return datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
