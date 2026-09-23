"""Connecting a mailbox from the app, and the first run.

Against the fake IMAP server, through the same HTTP API the page uses. The OS
keychain is replaced by a dictionary: what is tested is that the password goes
there and nowhere else, not the keychain itself.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage

import pytest

from bmail import accounts, secrets
from bmail.api import build_server
from bmail.config import Config
from bmail.errors import BmailError
from tests.fake_imap import FakeImapServer


def mail(n: int) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<acct{n}@imap.example>"
    message["Subject"] = f"Interview invitation {n}"
    message["From"] = "Talent <talent@acme-example.com>"
    message["To"] = "me@example.com"
    message["Date"] = "Mon, 14 Sep 2026 10:00:00 +0200"
    message.set_content("We would like to invite you to an interview next week.")
    return bytes(message)


@pytest.fixture
def keychain(monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(secrets, "keychain_status",
                        lambda: {"available": True, "backend": "memory", "hint": ""})
    monkeypatch.setattr(secrets, "set_secret", lambda name, pw: store.__setitem__(name, pw))
    monkeypatch.setattr(secrets, "delete_secret", lambda name: store.pop(name, None) is not None)
    monkeypatch.setattr(secrets, "get_secret", lambda name, user="": store.get(name, ""))
    return store


@pytest.fixture
def imap():
    with FakeImapServer() as server:
        yield server


@pytest.fixture
def live(tmp_path, keychain):
    home = tmp_path / "home"
    home.mkdir()
    server, app = build_server(Config.load(home=home), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", app
    server.shutdown()
    server.server_close()
    app.shutdown()


def call(base, app, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method="POST" if data else "GET")
    request.add_header("Authorization", f"Bearer {app.token}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def form(imap, **extra):
    return {"address": imap.user, "password": imap.password, "host": "127.0.0.1",
            "port": imap.port, "ssl": False, "since_days": 0, **extra}


def wait_job(base, app, job_id):
    deadline = time.time() + 30
    while time.time() < deadline:
        _, job = call(base, app, f"/api/jobs/{job_id}")
        if job["status"] in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


# --------------------------------------------------------------------------- #
# Presets and validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("address", "key", "host"), [
    ("someone@gmx.de", "gmx", "imap.gmx.net"),
    ("someone@googlemail.com", "gmail", "imap.gmail.com"),
    ("someone@web.de", "webde", "imap.web.de"),
    ("someone@uni-bremen.de", "custom", "imap.uni-bremen.de"),
])
def test_the_provider_is_recognised_from_the_address(address, key, host):
    preset = accounts.detect(address)
    assert (preset.key, preset.host) == (key, host)


def test_an_outlook_address_is_explained_not_failed():
    """Microsoft refuses passwords; trying one would just fail the login with
    an error that tells the person nothing."""
    with pytest.raises(BmailError, match="Microsoft"):
        accounts.build_spec({"address": "someone@hotmail.com", "password": "x"})


@pytest.mark.parametrize("fields", [
    {"address": "not an address"},
    {"address": "a@b.example", "port": "imap"},
    {"address": "a@b.example", "port": 70000},
    {"address": "a@b.example", "since_days": 42},
])
def test_bad_fields_are_refused_with_a_reason(fields):
    with pytest.raises(BmailError):
        accounts.build_spec(fields)


# --------------------------------------------------------------------------- #
# The connection test
# --------------------------------------------------------------------------- #


def test_the_connection_test_reports_what_it_found(live, imap):
    base, app = live
    imap.mailbox.add(mail(1))
    imap.mailbox.add(mail(2))
    status, result = call(base, app, "/api/accounts/check", form(imap))
    assert status == 200 and result["messages"] == 2
    assert not app.config.accounts, "testing must not save anything"


def test_a_wrong_password_says_so_plainly(live, imap):
    base, app = live
    status, result = call(base, app, "/api/accounts/check", form(imap, password="nope"))
    assert status == 400
    assert "refused that password" in result["error"]
    assert "b'" not in result["error"]


def test_an_unknown_server_says_so_plainly(live):
    base, app = live
    status, result = call(base, app, "/api/accounts/check",
                          {"address": "me@example.com", "password": "x",
                           "host": "no-such-host.invalid"})
    assert status == 400
    assert "could be found" in result["error"]


# --------------------------------------------------------------------------- #
# Adding and removing
# --------------------------------------------------------------------------- #


def test_adding_an_account_keeps_the_password_in_the_keychain_only(live, imap, keychain):
    base, app = live
    status, result = call(base, app, "/api/accounts", form(imap))
    assert status == 200, result
    assert keychain == {imap.user: imap.password}
    saved = (app.config.home / "accounts.json").read_text()
    assert imap.password not in saved
    assert json.loads(saved)["accounts"][0]["address"] == imap.user
    assert (app.config.home / "accounts.json").stat().st_mode & 0o077 == 0
    assert result["account"]["removable"] is True
    assert "password" not in result["account"]


def test_the_app_state_lists_an_added_account_without_its_password(live, imap):
    """The page reloads its state after connecting; that call once failed with
    a 500 as soon as any account existed, stranding the first run mid-way."""
    base, app = live
    call(base, app, "/api/accounts", form(imap))
    status, state = call(base, app, "/api/state")
    assert status == 200, state
    (entry,) = state["configured_accounts"]
    assert entry["name"] == imap.user and entry["removable"] is True
    assert "password" not in entry
    status, listing = call(base, app, "/api/accounts")
    assert status == 200 and listing["accounts"][0]["name"] == imap.user


def test_adding_an_account_starts_its_first_import(live, imap):
    base, app = live
    imap.mailbox.add(mail(1))
    imap.mailbox.add(mail(2))
    _, result = call(base, app, "/api/accounts", form(imap))
    job = wait_job(base, app, result["job"]["id"])
    assert job["status"] == "done", job.get("error")
    assert job["result"]["new"][imap.user] == 2
    assert job["result"]["sorted"] == 2


def test_the_account_survives_a_restart_and_is_synced(live, imap):
    base, app = live
    call(base, app, "/api/accounts", form(imap))
    reloaded = Config.load(home=app.config.home)
    assert [a["name"] for a in reloaded.accounts] == [imap.user]
    assert [a["name"] for a in app.syncer.live_accounts()] == [imap.user]


def test_a_failed_sign_in_saves_nothing(live, imap, keychain):
    base, app = live
    status, _ = call(base, app, "/api/accounts", form(imap, password="nope"))
    assert status == 400
    assert keychain == {}
    assert not (app.config.home / "accounts.json").exists()


def test_no_keychain_means_no_account_rather_than_a_password_on_disk(live, imap, monkeypatch):
    base, app = live
    monkeypatch.setattr(secrets, "keychain_status",
                        lambda: {"available": False, "backend": "fail",
                                 "hint": "no keychain service is running"})
    status, result = call(base, app, "/api/accounts", form(imap))
    assert status == 400 and "keychain" in result["error"]
    assert not (app.config.home / "accounts.json").exists()


def test_the_same_address_cannot_be_added_twice(live, imap):
    base, app = live
    call(base, app, "/api/accounts", form(imap))
    status, result = call(base, app, "/api/accounts", form(imap))
    assert status == 400 and "already connected" in result["error"]


def test_removing_an_account_forgets_it_but_keeps_its_mail(live, imap, keychain):
    base, app = live
    imap.mailbox.add(mail(1))
    _, added = call(base, app, "/api/accounts", form(imap))
    wait_job(base, app, added["job"]["id"])
    status, _ = call(base, app, f"/api/accounts/{imap.user}/remove", {})
    assert status == 200
    assert keychain == {}
    assert app.config.accounts == []
    assert json.loads((app.config.home / "accounts.json").read_text()) == {"accounts": []}
    _, state = call(base, app, "/api/state")
    assert state["messages"]["total"] == 1


def test_an_account_from_the_config_file_is_not_removed_from_the_app(tmp_path, keychain):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[[accounts]]\nname = "uni"\nprovider = "imap"\nhost = "imap.uni.example"\n'
        'username = "me"\n', encoding="utf-8")
    config = Config.load(home=home)
    with pytest.raises(BmailError, match="config.toml"):
        accounts.remove(config, "uni")


def test_the_config_file_wins_a_name_clash(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[[accounts]]\nname = "me@example.com"\nprovider = "imap"\nhost = "from-file"\n',
        encoding="utf-8")
    (home / "accounts.json").write_text(json.dumps({"accounts": [
        {"name": "me@example.com", "provider": "imap", "host": "from-app"},
        {"name": "other@example.com", "provider": "imap", "host": "from-app"},
    ]}), encoding="utf-8")
    config = Config.load(home=home)
    assert [(a["name"], a["host"]) for a in config.accounts] == [
        ("me@example.com", "from-file"), ("other@example.com", "from-app")]


# --------------------------------------------------------------------------- #
# First run
# --------------------------------------------------------------------------- #


def test_a_new_install_is_a_first_run_until_it_is_set_up(live):
    base, app = live
    _, state = call(base, app, "/api/state")
    assert state["setup"]["first_run"] is True
    call(base, app, "/api/setup", {"done": True})
    _, state = call(base, app, "/api/state")
    assert state["setup"]["first_run"] is False


def test_the_chosen_pack_is_kept_by_the_engine(live, tmp_path):
    """It used to live in the browser only, so background sync went on sorting
    with the config's pack while the page showed another."""
    base, app = live
    status, setup = call(base, app, "/api/setup", {"pack": "student-admissions"})
    assert status == 200 and setup["pack"] == "student-admissions"
    assert app.pack_name == "student-admissions"
    server, again = build_server(Config.load(home=app.config.home), port=0)
    try:
        assert again.pack_name == "student-admissions"
    finally:
        server.server_close()
        again.shutdown()


def test_an_unknown_pack_is_refused(live):
    base, app = live
    status, _ = call(base, app, "/api/setup", {"pack": "astrology"})
    assert status == 400


def test_sample_mail_can_be_tried_without_connecting_anything(live):
    base, app = live
    _, job = call(base, app, "/api/samples", {"no_llm": True})
    job = wait_job(base, app, job["id"])
    assert job["status"] == "done", job.get("error")
    assert job["result"]["new"]["sample"] > 0
    _, state = call(base, app, "/api/state")
    assert state["setup"]["first_run"] is False
