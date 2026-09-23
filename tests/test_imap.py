"""The IMAP connector, against a real IMAP server on a local socket.

The cursor is the thing worth testing. IMAP gives every message a UID that only
ever grows within one UIDVALIDITY, and incremental sync is "everything above
the highest UID I have seen". Get the arithmetic wrong by one and the next
message to arrive is skipped for ever, silently — the worst failure a mail tool
can have, because nothing ever tells you it happened.
"""

from __future__ import annotations

from email.message import EmailMessage

import pytest

from bmail.errors import IngestError
from bmail.ingest import ImapSource, ingest
from bmail.store import repo
from tests.fake_imap import FakeImapServer


def mail(n: int) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<m{n}@imap.example>"
    message["Subject"] = f"Message {n}"
    message["From"] = "Recruiter <jobs@acme.example>"
    message["To"] = "me@example.com"
    message["Date"] = "Mon, 14 Sep 2026 10:00:00 +0200"
    message.set_content(f"Body of message {n}.")
    return bytes(message)


def source(server: FakeImapServer, **kwargs) -> ImapSource:
    return ImapSource(
        "127.0.0.1", server.user, server.password,
        name="imap-test", port=server.port, use_ssl=False, **kwargs,
    )


def subjects(messages) -> list[str]:
    return [m.subject for m in messages]


@pytest.fixture
def server():
    with FakeImapServer() as fake:
        yield fake


def test_the_first_sync_reads_everything_and_records_where_it_stopped(server):
    for n in (1, 2, 3):
        server.mailbox.add(mail(n))
    src = source(server)
    assert subjects(src.fetch()) == ["Message 1", "Message 2", "Message 3"]
    assert src.cursor() == {"uidvalidity": "1000", "uidnext": 4}


def test_a_sync_that_finds_nothing_does_not_skip_the_next_message(server):
    """The bug this file exists for. A sync with nothing new used to advance the
    cursor by one anyway, so the next message to arrive — which is given exactly
    that UID — fell below it and was never fetched."""
    server.mailbox.add(mail(1))
    server.mailbox.add(mail(2))
    src = source(server)
    list(src.fetch())
    cursor = src.cursor()

    quiet = source(server)
    assert subjects(quiet.fetch(cursor)) == []
    cursor = quiet.cursor() or cursor

    server.mailbox.add(mail(3))
    later = source(server)
    assert subjects(later.fetch(cursor)) == ["Message 3"], "the next message was skipped"


def test_a_sync_of_an_empty_mailbox_does_not_skip_the_first_message(server):
    src = source(server)
    assert subjects(src.fetch()) == []
    cursor = src.cursor()
    server.mailbox.add(mail(1))
    assert subjects(source(server).fetch(cursor)) == ["Message 1"]


def test_only_new_messages_come_through_on_the_next_sync(server):
    server.mailbox.add(mail(1))
    src = source(server)
    list(src.fetch())
    server.mailbox.add(mail(2))
    server.mailbox.add(mail(3))
    again = source(server)
    assert subjects(again.fetch(src.cursor())) == ["Message 2", "Message 3"]


def test_a_changed_uidvalidity_rescans_the_whole_folder(server):
    for n in (1, 2):
        server.mailbox.add(mail(n))
    src = source(server)
    list(src.fetch())
    server.mailbox.renumber(2000)
    rescanned = source(server)
    assert subjects(rescanned.fetch(src.cursor())) == ["Message 1", "Message 2"]
    assert rescanned.cursor()["uidvalidity"] == "2000"


def test_uidvalidity_comes_from_select_on_servers_that_refuse_status():
    """RFC 3501: a client SHOULD NOT send STATUS for the mailbox it has selected,
    and some servers refuse. Reading UIDVALIDITY that way then returned nothing,
    so a renumbered mailbox was never noticed."""
    with FakeImapServer(refuse_status=True) as server:
        server.mailbox.add(mail(1))
        src = source(server)
        list(src.fetch())
        assert src.cursor()["uidvalidity"] == "1000"
        server.mailbox.renumber(2000)
        again = source(server)
        assert subjects(again.fetch(src.cursor())) == ["Message 1"]


def test_a_limit_takes_the_oldest_and_the_next_sync_resumes_after_them(server):
    for n in range(1, 6):
        server.mailbox.add(mail(n))
    src = source(server)
    assert subjects(src.fetch(limit=2)) == ["Message 1", "Message 2"]
    rest = source(server)
    assert subjects(rest.fetch(src.cursor())) == ["Message 3", "Message 4", "Message 5"]


def test_a_wrong_password_is_an_ingest_error(server):
    wrong = ImapSource("127.0.0.1", server.user, "not-it", name="imap-test",
                       port=server.port, use_ssl=False)
    with pytest.raises(IngestError, match="login"):
        list(wrong.fetch())


def test_ingest_counts_what_is_new_not_what_was_seen(conn, server):
    server.mailbox.add(mail(1))
    server.mailbox.add(mail(2))
    first = ingest(conn, source(server))
    assert (first.fetched, first.new) == (2, 2)
    # Reset the cursor, so the same two come through again: seen, but not new.
    repo.set_cursor(conn, "imap-test", {})
    conn.commit()
    again = ingest(conn, source(server))
    assert (again.fetched, again.new) == (2, 0)
