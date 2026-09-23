"""The first run, in a real browser, against the fake IMAP server.

A new install opens on the welcome instead of an empty Today; it asks what to
track, connects a mailbox, reads it, and hands over to Today. The keychain is a
dictionary here, patched into the same process as the server.
"""

from __future__ import annotations

import threading
from email.message import EmailMessage

import pytest

from bmail import secrets
from bmail.api import build_server
from bmail.config import Config
from tests.fake_imap import FakeImapServer

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
from tests.test_ui_browser import launch_chromium  # noqa: E402


def mail(n: int) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<wiz{n}@imap.example>"
    message["Subject"] = f"Interview invitation - Backend Developer {n}"
    message["From"] = "Talent <talent@acme-example.com>"
    message["To"] = "me@example.com"
    message["Date"] = "Mon, 14 Sep 2026 10:00:00 +0200"
    message.set_content("We would like to invite you to an interview for the Backend Developer role.")
    return bytes(message)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    store: dict[str, str] = {}
    monkeypatch.setattr(secrets, "keychain_status",
                        lambda: {"available": True, "backend": "memory", "hint": ""})
    monkeypatch.setattr(secrets, "set_secret", lambda name, pw: store.__setitem__(name, pw))
    monkeypatch.setattr(secrets, "delete_secret", lambda name: store.pop(name, None) is not None)
    monkeypatch.setattr(secrets, "get_secret", lambda name, user="": store.get(name, ""))
    with FakeImapServer() as imap:
        for n in (1, 2, 3):
            imap.mailbox.add(mail(n))
        home = tmp_path / "home"
        home.mkdir()
        server, app = build_server(Config.load(home=home), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with playwright_api.sync_playwright() as p:
                browser = launch_chromium(p)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors: list[str] = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.on("console",
                        lambda m: errors.append(m.text) if m.type == "error" else None)
                page.goto(f"{base}/?token={app.token}", wait_until="networkidle")
                page.wait_for_selector("#shell:not([hidden])", timeout=15000)
                try:
                    yield page, errors, imap, app, store
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            app.shutdown()
            thread.join(timeout=5)


def fill_connection(page, imap, password):
    page.fill(".connect-form input[type=email]", imap.user)
    page.click(".form-more summary")
    page.fill("input[aria-label=Server]", "127.0.0.1")
    page.fill("input[aria-label=Port]", str(imap.port))
    page.check(".form-plain input")
    page.select_option("select[aria-label='Read mail from']", "0")
    page.fill("input[aria-label=Password]", password)


def test_a_new_install_is_walked_from_nothing_to_today(fresh):
    page, errors, imap, app, store = fresh
    page.wait_for_selector(".wizard .choice")
    assert page.url.endswith("#/welcome"), "a new install opens on the welcome"

    page.click(".choice[data-pack='job-seeker']")
    fill_connection(page, imap, imap.password)
    page.click(".connect-form button[type=submit]")

    page.wait_for_selector(".wizard-facts", timeout=30000)
    assert "need" in page.inner_text(".headline") or "set up" in page.inner_text(".headline")
    assert store == {imap.user: imap.password}
    assert imap.password not in (app.config.home / "accounts.json").read_text()

    page.click(".wizard-foot button.primary")
    page.wait_for_function("() => location.hash === '#/today'")
    page.wait_for_selector(".page .headline")
    assert "welcome" not in page.url
    assert not errors


def test_a_wrong_password_is_answered_in_the_form(fresh):
    page, errors, imap, _app, store = fresh
    page.click(".choice[data-pack='job-seeker']")
    fill_connection(page, imap, "not-it")
    page.click(".connect-form button[type=submit]")
    page.wait_for_selector(".form-status[data-tone=error]")
    assert "refused that password" in page.inner_text(".form-status[data-tone=error]")
    assert store == {}
    assert page.url.endswith("#/welcome"), "the form stays open to try again"
    assert all("400" in e for e in errors), errors   # the refused sign-in itself


def test_skipping_the_welcome_does_not_bring_it_back(fresh):
    page, errors, _imap, _app, _store = fresh
    page.click("text=Skip setup")
    page.wait_for_function("() => location.hash === '#/today'")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    page.wait_for_timeout(300)
    assert not page.url.endswith("#/welcome")
    assert not errors


def test_a_mailbox_can_be_added_and_removed_from_setup(fresh):
    page, errors, imap, app, store = fresh
    page.click("text=Skip setup")
    page.wait_for_function("() => location.hash === '#/today'")
    page.evaluate("location.hash = '#/setup'")
    page.click("text=Add a mailbox")
    page.wait_for_selector(".sheet .connect-form")
    fill_connection(page, imap, imap.password)
    page.click(".sheet button[type=submit]")
    page.wait_for_selector(".sheet", state="detached", timeout=15000)
    page.wait_for_function(
        f"() => document.querySelector('#sync-accounts').innerText.includes('{imap.user}')",
        timeout=15000)

    page.click(f"button[aria-label='Remove {imap.user}']")
    page.click(".sheet button.primary")
    page.wait_for_function(
        f"() => !document.querySelector('#sync-accounts').innerText.includes('{imap.user}')",
        timeout=15000)
    assert store == {}
    assert app.config.accounts == []
    assert not errors
