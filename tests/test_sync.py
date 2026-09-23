"""Background sync: polling on an interval, and IMAP IDLE.

The IDLE tests run against the fake IMAP server, which really does park the
connection and push `* N EXISTS` when a message is added — so what is tested is
the watcher reading the protocol, not a mock agreeing with it.
"""

from __future__ import annotations

import threading
import time
from email.message import EmailMessage

import pytest

from bmail.api import BmailApp
from bmail.config import Config
from bmail.errors import BmailError
from bmail.store import repo
from bmail.store.db import connect
from bmail.sync import IdleWatcher, Syncer
from tests.fake_imap import FakeImapServer


def mail(n: int) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<sync{n}@imap.example>"
    message["Subject"] = f"Interview slot {n}"
    message["From"] = "Recruiter <jobs@acme.example>"
    message["To"] = "me@example.com"
    message["Date"] = "Mon, 14 Sep 2026 10:00:00 +0200"
    message.set_content(f"Could you do Thursday? ({n})")
    return bytes(message)


def imap_account(server: FakeImapServer, name: str = "uni") -> dict:
    return {"name": name, "provider": "imap", "host": "127.0.0.1", "port": server.port,
            "username": server.user, "password": server.password, "ssl": False}


def make_app(tmp_path, accounts, **syncer) -> BmailApp:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    app = BmailApp(Config(home=home, db_path=home / "bmail.sqlite3", accounts=accounts))
    options = {"first_delay": 0.05, "debounce": 0.1, "busy_retry": 0.1, "use_llm": False}
    options.update(syncer)
    app.syncer = Syncer(app, **options)
    return app


def wait_for(condition, timeout: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def stored(app: BmailApp) -> int:
    conn = connect(app.config.database, read_only=True)
    try:
        return repo.count_messages(conn)["total"]
    finally:
        conn.close()


@pytest.fixture
def server():
    with FakeImapServer() as fake:
        yield fake


# --------------------------------------------------------------------------- #
# The interval
# --------------------------------------------------------------------------- #


def test_the_interval_survives_a_restart(tmp_path):
    app = make_app(tmp_path, [])
    assert app.syncer.minutes == 5
    app.syncer.set_interval(15)
    app.shutdown()
    again = make_app(tmp_path, [])
    assert again.syncer.minutes == 15
    again.shutdown()


@pytest.mark.parametrize("bad", [7, -1, "soon", None])
def test_an_interval_outside_the_choices_is_refused(tmp_path, bad):
    app = make_app(tmp_path, [])
    with pytest.raises(BmailError):
        app.syncer.set_interval(bad)
    app.shutdown()


def test_files_are_not_polled(tmp_path):
    app = make_app(tmp_path, [{"name": "archive", "provider": "file", "path": "x.mbox"}])
    assert app.syncer.live_accounts() == []
    assert app.syncer.status()["accounts"] == []
    app.shutdown()


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #


def test_opening_the_app_checks_and_sorts_straight_away(tmp_path, server):
    server.mailbox.add(mail(1))
    server.mailbox.add(mail(2))
    app = make_app(tmp_path, [imap_account(server)])
    app.syncer.start()
    try:
        wait_for(lambda: app.syncer.generation >= 1, what="the first check")
        status = app.syncer.status()
        assert status["last"]["new"] == 2
        assert status["last"]["sorted"] == 2
        assert status["last_kind"] == "sync"
        assert status["accounts"][0]["status"] == "ok"
    finally:
        app.shutdown()


def test_a_check_that_finds_nothing_does_not_announce_anything(tmp_path, server):
    server.mailbox.add(mail(1))
    app = make_app(tmp_path, [imap_account(server)])
    app.syncer.start()
    try:
        wait_for(lambda: app.syncer.generation == 1, what="the first check")
        runs = app.syncer.last_run
        app.syncer.submit(app.syncer.live_accounts())
        wait_for(lambda: app.syncer.last_run != runs, what="a second check")
        assert app.syncer.generation == 1
        assert app.syncer.last["new"] == 0
    finally:
        app.shutdown()


def test_off_means_no_checks_at_all(tmp_path, server):
    server.mailbox.add(mail(1))
    app = make_app(tmp_path, [imap_account(server)])
    app.syncer.set_interval(0)
    app.syncer.start()
    try:
        time.sleep(0.5)
        assert stored(app) == 0
        assert app.syncer.status()["accounts"][0]["mode"] == "off"
        assert server.logins == 0
    finally:
        app.shutdown()


def test_switching_on_checks_straight_away(tmp_path, server):
    server.mailbox.add(mail(1))
    app = make_app(tmp_path, [imap_account(server)])
    app.syncer.set_interval(0)
    app.syncer.start()
    try:
        app.syncer.set_interval(30)
        wait_for(lambda: stored(app) == 1, what="the check after switching on")
    finally:
        app.shutdown()


def test_a_check_waits_for_the_worker_instead_of_queueing(tmp_path, server):
    """One writer. A check due while the worker is busy must wait for it, not
    pile up behind it."""
    server.mailbox.add(mail(1))
    app = make_app(tmp_path, [imap_account(server)])
    release = threading.Event()
    app.jobs.submit("triage", lambda job: release.wait(5) and {})
    app.syncer.start()
    try:
        time.sleep(0.4)
        assert [j.kind for j in app.jobs.list()] == ["triage"], "a check was queued"
        release.set()
        wait_for(lambda: stored(app) == 1, what="the check after the worker freed up")
    finally:
        release.set()
        app.shutdown()


def test_an_account_that_fails_is_reported_and_the_others_still_sync(tmp_path, server):
    server.mailbox.add(mail(1))
    broken = {**imap_account(server, "broken"), "password": "wrong"}
    app = make_app(tmp_path, [broken, imap_account(server, "fine")])
    app.syncer.start()
    try:
        wait_for(lambda: app.syncer.last_run is not None, what="the first check")
        accounts = {a["name"]: a for a in app.syncer.status()["accounts"]}
        assert accounts["broken"]["status"] == "error"
        assert "login" in accounts["broken"]["error"]
        assert accounts["fine"]["status"] == "ok"
    finally:
        app.shutdown()


def test_an_expired_sign_in_is_shown_as_such(tmp_path):
    app = make_app(tmp_path, [{"name": "gmail", "provider": "gmail",
                                "client_secret_file": "c.json", "token_file": "t.json"}])
    app.syncer.record({"fetched": {}, "new": {}, "errors": {"gmail": "sign in again"},
                       "sign_in": ["gmail"]}, kind="sync")
    (account,) = app.syncer.status()["accounts"]
    assert account["status"] == "sign-in"
    assert app.syncer.generation == 0
    app.shutdown()


def test_a_manual_check_pushes_the_next_automatic_one_back(tmp_path):
    ticks = [100.0]
    app = make_app(tmp_path, [], clock=lambda: ticks[0])
    app.syncer._due = 101.0
    app.syncer.record({"fetched": {}, "new": {}}, kind="ingest")
    assert app.syncer._due == 100.0 + 5 * 60
    app.shutdown()


# --------------------------------------------------------------------------- #
# IMAP IDLE
# --------------------------------------------------------------------------- #


def test_new_mail_arrives_by_idle_without_waiting_for_the_interval(tmp_path, server):
    app = make_app(tmp_path, [imap_account(server)], first_delay=3600)
    app.syncer.set_interval(60)  # the next poll is an hour away
    app.syncer.start()
    try:
        wait_for(lambda: app.syncer.status()["accounts"][0]["mode"] == "push",
                 what="the connection to park in IDLE")
        server.mailbox.add(mail(1))
        wait_for(lambda: stored(app) == 1, what="the pushed message to be fetched")
        wait_for(lambda: app.syncer.generation == 1, what="the check to be recorded")
        assert app.syncer.last["sorted"] == 1
    finally:
        app.shutdown()


def test_idle_keeps_listening_after_the_first_message(tmp_path, server):
    app = make_app(tmp_path, [imap_account(server)], first_delay=3600)
    app.syncer.set_interval(60)
    app.syncer.start()
    try:
        wait_for(lambda: app.syncer.status()["accounts"][0]["mode"] == "push", what="IDLE")
        server.mailbox.add(mail(1))
        wait_for(lambda: stored(app) == 1, what="the first message")
        server.mailbox.add(mail(2))
        wait_for(lambda: stored(app) == 2, what="the second message")
    finally:
        app.shutdown()


def test_a_server_without_idle_falls_back_to_polling(tmp_path):
    with FakeImapServer(idle=False) as server:
        app = make_app(tmp_path, [imap_account(server)], first_delay=3600)
        app.syncer.start()
        try:
            watcher = app.syncer._watchers["uni"]
            wait_for(lambda: watcher.mode == "unsupported", what="the watcher to give up")
            assert app.syncer.status()["accounts"][0]["mode"] == "poll"
            assert not any(line.split()[1:2] == ["IDLE"] for line in server.log)
        finally:
            app.shutdown()


def test_idle_is_renewed_on_a_fresh_connection(server):
    """RFC 2177: re-issue IDLE before the server's 30-minute timeout. After a
    read times out the connection's reader cannot be trusted, so renewal is a
    new connection, not a new command on the old one."""
    arrived: list[int] = []
    watcher = IdleWatcher(imap_account(server), resolve=dict,
                          on_mail=lambda: arrived.append(1), renew=0.3)
    watcher.start()
    try:
        wait_for(lambda: server.logins >= 3, what="IDLE to be renewed twice")
        assert watcher.mode == "idle"
        server.mailbox.add(mail(1))
        wait_for(lambda: arrived, what="mail to be noticed after renewal")
    finally:
        watcher.stop()
    assert not watcher.alive


def test_mail_that_lands_while_reconnecting_is_not_missed(server):
    """IDLE reports only what arrives while it runs. A message delivered in the
    gap between one connection and the next used to go unannounced until the
    next poll; the watcher now notices UIDNEXT moved while it was away."""
    arrived: list[int] = []
    watcher = IdleWatcher(imap_account(server), resolve=dict,
                          on_mail=lambda: arrived.append(1))
    watcher._uidnext = server.mailbox.next_uid  # where the last connection left off
    server.mailbox.add(mail(1))                 # delivered while disconnected
    watcher.start()
    try:
        wait_for(lambda: arrived, what="the gap to be noticed")
        assert watcher.mode == "idle"
    finally:
        watcher.stop()


def test_stopping_the_app_releases_idle_promptly(tmp_path, server):
    app = make_app(tmp_path, [imap_account(server)], first_delay=3600)
    app.syncer.start()
    wait_for(lambda: app.syncer.status()["accounts"][0]["mode"] == "push", what="IDLE")
    watcher = app.syncer._watchers["uni"]
    started = time.monotonic()
    app.shutdown()
    assert time.monotonic() - started < 3
    assert not watcher.alive
    assert not app.syncer.running


def test_switching_off_closes_the_idle_connection(tmp_path, server):
    app = make_app(tmp_path, [imap_account(server)], first_delay=3600)
    app.syncer.start()
    try:
        wait_for(lambda: app.syncer.status()["accounts"][0]["mode"] == "push", what="IDLE")
        watcher = app.syncer._watchers["uni"]
        app.syncer.set_interval(0)
        wait_for(lambda: not watcher.alive, what="the watcher to stop")
        assert app.syncer._watchers == {}
    finally:
        app.shutdown()


def test_a_wrong_password_backs_off_rather_than_hammering_the_server(server):
    account = {**imap_account(server), "password": "wrong"}
    watcher = IdleWatcher(account, resolve=dict, on_mail=lambda: None, backoff=(0.2, 0.4))
    watcher.start()
    try:
        wait_for(lambda: watcher.mode == "error", what="the login to fail")
        time.sleep(0.5)
        attempts = sum(1 for line in server.log if " LOGIN " in line)
        assert 1 <= attempts <= 4
        assert "login" in watcher.error
    finally:
        watcher.stop()


# --------------------------------------------------------------------------- #
# Plain IMAP is for this machine only
# --------------------------------------------------------------------------- #


def test_plain_imap_is_refused_for_a_remote_host():
    from bmail.ingest import build_source

    with pytest.raises(BmailError, match="this machine"):
        build_source({"name": "x", "provider": "imap", "host": "imap.example.com",
                      "username": "me", "password": "p", "ssl": False})
