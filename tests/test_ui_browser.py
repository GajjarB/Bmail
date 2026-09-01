"""The UI, driven in a real browser.

Skipped unless Playwright and a Chromium build are both available, because the
suite must stay runnable on a plain checkout. When it does run, it is the only
thing that proves the page actually works rather than merely parses.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from sift.api import build_server
from sift.config import Config
from sift.ingest import FileSource, ingest
from sift.samples import write_mbox
from sift.store import repo
from sift.store.db import connect

playwright_api = pytest.importorskip("playwright.sync_api", reason="playwright not installed")


def _chromium_path() -> str | None:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    if not root.is_dir():
        return None
    for candidate in sorted(root.glob("chromium-*/chrome-linux/chrome")):
        return str(candidate)
    for candidate in sorted(root.glob("chromium*/chrome-linux/chrome")):
        return str(candidate)
    return None


@pytest.fixture(scope="module")
def ui(tmp_path_factory):
    from sift.classify.stage2 import Classifier
    from sift.packs import load_pack
    from sift.pipeline import Pipeline

    tmp_path = tmp_path_factory.mktemp("ui")
    home = tmp_path / "home"
    home.mkdir()
    config = Config(home=home, db_path=home / "sift.sqlite3")
    mbox, _labels = write_mbox(tmp_path, applications=10, noise=12)

    conn = connect(config.database)
    ingest(conn, FileSource(mbox, name="demo", own_addresses={"you@example.com"}))
    pack = load_pack("job-seeker")
    Pipeline(pack, classifier=Classifier(pack), use_llm=False).triage(
        conn, repo.iter_messages(conn)
    )
    conn.close()

    server, app = build_server(config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", app, config
    server.shutdown()
    server.server_close()
    app.shutdown()
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def page_factory(ui):
    executable = _chromium_path()
    if executable is None:
        pytest.skip("no Chromium build available")
    base, app, config = ui
    with playwright_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=executable, args=["--no-sandbox"])

        def make():
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(f"{base}/?token={app.token}", wait_until="networkidle")
            page.wait_for_selector("#shell:not([hidden])", timeout=15000)
            return page, errors, config

        yield make
        browser.close()


def test_the_app_boots_and_hides_the_token(page_factory):
    page, errors, _config = page_factory()
    assert "token=" not in page.url, "the token must not stay in the address bar"
    assert page.evaluate("sessionStorage.getItem('sift.token')")
    assert "messages" in page.inner_text("#status-store")
    assert not errors
    page.close()


def test_the_brief_renders_evidence(page_factory):
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('sift.briefDays','3650')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    assert page.locator(".card").count() >= 5
    assert page.locator(".evidence").count() > 0, "every extracted fact should show its source"
    assert not errors
    page.close()


def test_an_empty_brief_offers_a_way_forward(page_factory):
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('sift.briefDays','1')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    if page.locator(".empty").count():
        assert page.locator(".empty button").count() == 1, "a dead end is a bug"
    assert not errors
    page.close()


def test_cases_open_a_timeline(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="cases"]')
    page.wait_for_selector("tbody tr")
    page.locator("tbody tr").first.click()
    page.wait_for_selector(".timeline li")
    assert page.locator(".timeline li").count() >= 1
    assert not errors
    page.close()


def test_messages_search_and_page(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="messages"]')
    page.wait_for_selector("tbody tr")
    before = page.locator("tbody tr").count()
    page.fill('input[type="search"]', "Bewerbung")
    page.press('input[type="search"]', "Enter")
    page.wait_for_timeout(700)
    assert page.locator("tbody tr").count() <= before
    assert not errors
    page.close()


def test_a_correction_round_trips_to_the_store(page_factory):
    page, errors, config = page_factory()
    page.click('[data-tab="messages"]')
    page.wait_for_selector("tbody tr")
    page.locator("tbody tr").first.click()
    page.wait_for_selector(".detail select")
    message_id = page.evaluate(
        "new URLSearchParams(location.hash.split('?')[1]).get('id')"
    )
    page.select_option(".detail select", "personal")
    page.click(".detail button:has-text('Save correction')")
    page.wait_for_selector("#toast:not([hidden])")

    conn = connect(config.database)
    try:
        verdict = repo.get_verdict(conn, message_id, "job-seeker")
        assert verdict is not None
        assert verdict.category == "personal"
        assert verdict.stage.value == "manual"
    finally:
        conn.close()
    assert not errors
    page.close()


def test_a_triage_job_runs_from_the_ui(page_factory):
    page, errors, _config = page_factory()
    page.click("#btn-triage")
    page.wait_for_selector("#toast:not([hidden])", timeout=60000)
    assert "Triage" in page.inner_text("#toast") or "Nothing" in page.inner_text("#toast")
    assert not errors
    page.close()


def test_keyboard_navigation(page_factory):
    page, errors, _config = page_factory()
    page.keyboard.press("2")
    page.wait_for_timeout(400)
    assert page.url.endswith("#/cases")
    page.keyboard.press("5")
    page.wait_for_timeout(400)
    assert "settings" in page.url
    assert not errors
    page.close()


def test_no_external_requests_are_made(page_factory, ui):
    """A local-first tool must not phone anywhere, including for a font."""
    base, _app, _config = ui
    page, errors, _ = page_factory()
    external: list[str] = []
    page.on("request", lambda r: external.append(r.url) if not r.url.startswith(base) and not r.url.startswith("data:") else None)
    for tab in ("brief", "cases", "messages", "review", "settings"):
        page.click(f'[data-tab="{tab}"]')
        page.wait_for_timeout(400)
    assert external == [], f"the UI reached outside the machine: {external}"
    assert not errors
    page.close()
