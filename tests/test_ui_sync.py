"""Mail arriving on its own, seen from the page.

A live IMAP account on the fake server, the daemon syncing in the background,
and a real browser: a message delivered to the server has to end up counted in
the sidebar without anyone pressing anything.
"""

from __future__ import annotations

import threading
from email.message import EmailMessage

import pytest

from bmail.api import build_server
from bmail.config import Config
from bmail.sync import Syncer
from tests.fake_imap import FakeImapServer

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
from tests.test_ui_browser import _chromium_path  # noqa: E402


def mail(n: int) -> bytes:
    message = EmailMessage()
    message["Message-ID"] = f"<ui-sync{n}@imap.example>"
    message["Subject"] = f"Your application, part {n}"
    message["From"] = "Talent <careers@acme.example>"
    message["To"] = "me@example.com"
    message["Date"] = "Tue, 15 Sep 2026 09:30:00 +0200"
    message.set_content("Thanks for applying. We'll be in touch within a week.")
    return bytes(message)


@pytest.fixture
def live_page(tmp_path):
    executable = _chromium_path()
    if executable is None:
        pytest.skip("no Chromium build available")
    with FakeImapServer() as imap:
        imap.mailbox.add(mail(1))
        home = tmp_path / "home"
        home.mkdir()
        config = Config(home=home, db_path=home / "bmail.sqlite3", accounts=[{
            "name": "uni", "provider": "imap", "host": "127.0.0.1", "port": imap.port,
            "username": imap.user, "password": imap.password, "ssl": False,
        }])
        server, app = build_server(config, port=0)
        app.syncer = Syncer(app, first_delay=0.05, debounce=0.1, busy_retry=0.1, use_llm=False)
        app.syncer.start()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=executable, args=["--no-sandbox"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.goto(f"{base}/?token={app.token}", wait_until="networkidle")
            page.wait_for_selector("#shell:not([hidden])", timeout=15000)
            try:
                yield page, errors, imap
            finally:
                browser.close()
                server.shutdown()
                server.server_close()
                app.shutdown()
                thread.join(timeout=5)


def poll_until(page, script: str, timeout: float = 15000) -> None:
    """Ask the page to check now rather than wait out its 15-second timer.

    Predicates are passed as functions, not expressions: Playwright re-runs an
    expression with eval(), which this page's CSP rightly refuses."""
    page.wait_for_function(f"async () => {{ await Sync.poll(); return {script}; }}",
                           timeout=timeout, polling=250)


def test_new_mail_appears_without_pressing_anything(live_page):
    page, errors, imap = live_page
    poll_until(page, "document.querySelector('#sync-line').dataset.tone === 'live'")
    assert "Checked" in page.inner_text("#sync-line")
    page.wait_for_function(
        "() => document.querySelector('#rail-meta').innerText.includes('1 message read')")

    imap.mailbox.add(mail(2))
    poll_until(page, "document.querySelector('#rail-meta').innerText.includes('2 messages read')")
    page.wait_for_selector("#toast:not([hidden])")
    assert "1 new message" in page.inner_text("#toast")
    assert not errors


def test_setup_shows_each_account_and_can_turn_checks_off(live_page):
    page, errors, _imap = live_page
    poll_until(page, "Sync.status.accounts[0].mode === 'push'")
    page.evaluate("location.hash = '#/setup'")
    page.wait_for_selector("#sync-accounts .sync-acct")
    assert "Live" in page.inner_text("#sync-accounts")

    page.select_option("select[aria-label='Check automatically']", "0")
    page.wait_for_function(
        "() => document.querySelector('#sync-line').innerText.includes('Automatic checks off')")
    poll_until(page, "Sync.status.accounts[0].mode === 'off'")
    page.wait_for_function(
        "() => document.querySelector('#sync-accounts').innerText.includes('Only when you check')")
    assert not errors
