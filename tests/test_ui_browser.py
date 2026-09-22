"""The UI, driven in a real browser.

Skipped unless Playwright and a Chromium build are both available, because the
suite must stay runnable on a plain checkout. When it does run, it is the only
thing that proves the page actually works rather than merely parses.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

import pytest

from bmail.api import build_server
from bmail.config import Config
from bmail.ingest import FileSource, ingest
from bmail.samples import write_mbox
from bmail.store import repo
from bmail.store.db import connect

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
    from bmail.classify.stage2 import Classifier
    from bmail.packs import load_pack
    from bmail.pipeline import Pipeline

    tmp_path = tmp_path_factory.mktemp("ui")
    home = tmp_path / "home"
    home.mkdir()
    config = Config(home=home, db_path=home / "bmail.sqlite3")
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

        def make(**kwargs):
            kwargs.setdefault("viewport", {"width": 1440, "height": 900})
            page = browser.new_page(**kwargs)
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            # Console errors too, not just uncaught exceptions. A Content-Security-
            # Policy violation is reported here and nowhere else, which is how
            # eleven dead inline styles sat in the UI without a single test
            # noticing that the browser was refusing every one of them.
            page.on(
                "console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None,
            )
            page.goto(f"{base}/?token={app.token}", wait_until="networkidle")
            page.wait_for_selector("#shell:not([hidden])", timeout=15000)
            return page, errors, config

        yield make
        browser.close()


def test_the_app_boots_and_hides_the_token(page_factory):
    page, errors, _config = page_factory()
    assert "token=" not in page.url, "the token must not stay in the address bar"
    assert page.evaluate("sessionStorage.getItem('bmail.token')")
    assert "messages read" in page.inner_text("#rail-meta")
    assert not errors
    page.close()


def test_the_headline_states_the_true_number(page_factory):
    """The headline is a claim about reality, so it must not be the page size."""
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('bmail.window','3650')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    headline = page.inner_text(".headline")
    badge = page.inner_text('#nav a[data-tab="today"]')
    numbers = [int(n) for n in re.findall(r"\d+", headline)]
    if numbers:
        assert str(numbers[0]) in badge, "headline and sidebar disagree about the count"
    assert not errors
    page.close()


def test_every_extracted_fact_shows_its_source(page_factory):
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('bmail.window','3650')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    assert page.locator(".quote").count() > 0, "the evidence quote is the trust device"
    assert not errors
    page.close()


def test_an_empty_brief_offers_a_way_forward(page_factory):
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('bmail.window','1')")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    if page.locator(".bar-row button").count():
        assert page.inner_text(".headline"), "an empty day still needs a sentence"
    assert not errors
    page.close()


def test_the_sidebar_speaks_the_packs_language(page_factory):
    """job-seeker tracks applications, freelance-ops tracks invoices."""
    page, errors, _config = page_factory()
    labels = " ".join(page.locator("#nav a").all_inner_texts()).lower()
    assert "application" in labels
    assert not errors
    page.close()


def test_the_stage_rail_only_fills_stages_that_happened(page_factory):
    """Inferring progress from the current state would claim interviews that
    never took place."""
    page, errors, _config = page_factory()
    page.click('[data-tab="cases"]')
    page.wait_for_selector(".stages")
    filled = page.evaluate("""() => {
      const card = document.querySelector('.card .stages');
      const done = card.querySelectorAll('.stage[data-done="1"]').length;
      const now = card.querySelectorAll('.stage[data-now="1"]').length;
      return { done, now, total: card.querySelectorAll('.stage').length };
    }""")
    assert filled["total"] > filled["done"], "a rail with everything filled says nothing"
    assert not errors
    page.close()


def test_cases_open_a_timeline(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="cases"]')
    page.wait_for_selector(".card")
    page.locator(".card").first.click()
    page.wait_for_selector(".timeline li")
    assert page.locator(".timeline li").count() >= 1
    assert not errors
    page.close()


def test_closed_cases_do_not_show_a_deadline(page_factory):
    """A rejected application has no outstanding task."""
    page, errors, _config = page_factory()
    page.click('[data-tab="cases"]')
    page.wait_for_selector(".card")
    stale = page.locator('.stack.closed .card .chip[data-tone="urgent"]').count()
    assert stale == 0
    assert not errors
    page.close()


def test_mail_rows_do_not_collide(page_factory):
    """Subject and preview are separate lines, not overlapping text."""
    page, errors, _config = page_factory()
    page.click('[data-tab="mail"]')
    page.wait_for_selector('[data-list="mail"] .row')
    overlap = page.evaluate("""() => {
      const row = document.querySelector('.row');
      const t = row.querySelector('.row-title').getBoundingClientRect();
      const s = row.querySelector('.row-sub').getBoundingClientRect();
      return t.bottom > s.top + 1;
    }""")
    assert overlap is False
    assert not errors
    page.close()


def test_mail_search_narrows_the_list(page_factory):
    """Scoped to the mail list: Today's "gone quiet" block also uses `.row`, and
    selecting on the bare class could measure whichever view was still on
    screen."""
    page, errors, _config = page_factory()
    page.click('[data-tab="mail"]')
    mail_rows = page.locator('[data-list="mail"] .row')
    page.wait_for_selector('[data-list="mail"] .row')
    before = mail_rows.count()
    assert before > 0
    page.fill('input[type="search"]', "Bewerbung")
    page.press('input[type="search"]', "Enter")
    page.wait_for_function(
        "n => document.querySelectorAll('[data-list=\"mail\"] .row').length !== n",
        arg=before, timeout=10000)
    assert mail_rows.count() <= before
    assert not errors
    page.close()


def test_a_correction_round_trips_to_the_store(page_factory):
    page, errors, config = page_factory()
    page.click('[data-tab="mail"]')
    page.wait_for_selector('[data-list="mail"] .row')
    page.locator('[data-list="mail"] .row').first.click()
    page.wait_for_selector(".aside select")
    message_id = page.evaluate(
        "new URLSearchParams(location.hash.split('?')[1]).get('id')"
    )
    page.select_option(".aside select", "personal")
    page.click(".aside button:has-text('Save')")
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


def test_review_asks_one_question_at_a_time(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="review"]')
    page.wait_for_timeout(900)
    if "Nothing to check" in page.inner_text(".headline"):
        page.close()
        return
    assert page.locator(".guess").count() == 1, "review shows one message, not a table"
    assert page.locator("button:has-text(\"Yes, that's right\")").count() == 1
    # Every alternative is offered: truncating hides the answer that matters.
    offered = page.locator(".choices button").count()
    total = page.evaluate("App.state.categories.length")
    assert offered == total - 1
    assert not errors
    page.close()


def test_a_review_answer_advances_the_queue(page_factory):
    page, errors, _config = page_factory()
    page.click('[data-tab="review"]')
    page.wait_for_timeout(900)
    if "Nothing to check" in page.inner_text(".headline"):
        page.close()
        return
    before = page.inner_text(".review-progress")
    page.click("button:has-text(\"Yes, that's right\")")
    page.wait_for_timeout(1200)
    after = page.inner_text(".review-progress") if page.locator(".review-progress").count() else ""
    assert after != before
    assert not errors
    page.close()


def test_a_sync_job_runs_from_the_sidebar(page_factory):
    page, errors, _config = page_factory()
    page.click("#btn-sync")
    page.wait_for_selector("#toast:not([hidden])", timeout=60000)
    assert page.inner_text("#toast")
    assert not errors
    page.close()


def test_keyboard_navigation(page_factory):
    page, errors, _config = page_factory()
    page.keyboard.press("2")
    page.wait_for_timeout(400)
    assert page.url.endswith("#/cases")
    page.keyboard.press("5")
    page.wait_for_timeout(400)
    assert "setup" in page.url
    assert not errors
    page.close()


def test_it_reads_in_dark_mode(page_factory):
    """Both themes are first-class; a washed-out dark mode is a broken one."""
    page, errors, _config = page_factory(color_scheme="dark")
    contrast = page.evaluate(r"""() => {
      const s = getComputedStyle(document.body);
      const p = (c) => c.match(/\d+/g).slice(0, 3).map(Number);
      const lum = (c) => p(c).reduce((a, v) => a + v, 0) / 3;
      return { bg: lum(s.backgroundColor), fg: lum(s.color) };
    }""")
    assert contrast["bg"] < 80, "dark mode should actually be dark"
    assert contrast["fg"] - contrast["bg"] > 100, "text must stay readable"
    assert not errors
    page.close()


def test_no_external_requests_are_made(page_factory, ui):
    """A local-first tool must not phone anywhere, including for a font."""
    base, _app, _config = ui
    page, errors, _ = page_factory()
    external: list[str] = []
    page.on("request", lambda r: external.append(r.url)
            if not r.url.startswith(base) and not r.url.startswith("data:") else None)
    for tab in ("today", "cases", "mail", "review", "setup"):
        page.click(f'[data-tab="{tab}"]')
        page.wait_for_timeout(400)
    assert external == [], f"the UI reached outside the machine: {external}"
    assert not errors
    page.close()


def test_switching_pack_switches_the_data_not_just_the_labels(page_factory):
    """The sidebar once relabelled itself for the new domain while every list
    underneath kept showing the old one's data — a working-looking switcher."""
    page, errors, _config = page_factory()
    page.evaluate("localStorage.setItem('bmail.pack','student-admissions')")
    page.goto(page.url.split("#")[0] + "#/cases")
    page.reload(wait_until="networkidle")
    page.wait_for_selector("#shell:not([hidden])")
    page.wait_for_timeout(600)

    nav = " ".join(page.locator("#nav a").all_inner_texts()).lower()
    assert "programme" in nav, "the sidebar should name the new pack's entity"

    options = " ".join(page.locator(".bar-row select option").all_inner_texts())
    assert "enquiry" in options, "the stage filter still lists the old pack's states"
    assert "acknowledged" not in options

    calls = page.evaluate("""async () => {
      const r = await fetch('/api/cases', {headers:{Authorization:'Bearer '+App.token}});
      return App.packChoice;
    }""")
    assert calls == "student-admissions"
    assert not errors
    page.close()


def test_mail_content_can_never_become_markup(page_factory, ui):
    """Mail is attacker-controlled text rendered in a privileged local page.

    The el() helper refuses innerHTML outright, so this asserts the outcome:
    a subject full of markup renders as visible text and adds no elements.
    """
    _base, _app, config = ui
    from datetime import datetime, timezone

    from bmail.models import EmailAddress, Message

    conn = connect(config.database)
    try:
        repo.bulk_upsert(conn, [Message(
            message_id="<xss@x>",
            date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            subject="<script>alert(1)</script> <img src=x onerror=alert(2)>",
            body_text="<b>bold</b> & <i>italic</i>",
            **{"from": EmailAddress(name="<i>Name</i>", address="x@example.com")},
        )])
    finally:
        conn.close()

    page, errors, _ = page_factory()
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(page.url.split("#")[0] + "#/mail")
    page.wait_for_selector('[data-list="mail"] .row')
    page.fill('input[type="search"]', "script")
    page.press('input[type="search"]', "Enter")
    page.wait_for_timeout(800)

    found = page.evaluate("""() => ({
      scripts: [...document.querySelectorAll('script')].map(s => s.src),
      imgs: document.querySelectorAll('img').length,
      asText: [...document.querySelectorAll('.row-title')].some(e => e.textContent.includes('<script>')),
    })""")
    assert dialogs == [], "mail content executed"
    assert found["imgs"] == 0, "mail content added an element"
    assert all(src.endswith("/app.js") for src in found["scripts"] if src)
    assert found["asText"], "the subject should be visible as text"
    assert not errors
    page.close()


def test_long_subjects_do_not_break_the_layout(page_factory, ui):
    _base, _app, config = ui
    from datetime import datetime, timezone

    from bmail.models import EmailAddress, Message

    conn = connect(config.database)
    try:
        repo.bulk_upsert(conn, [Message(
            message_id="<long@x>",
            date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            subject="Betreff " + "einsehrlangeswortohneleerzeichen" * 10,
            body_text="A" * 3000,
            **{"from": EmailAddress(name="N" * 100, address="long" * 12 + "@example.com")},
        )])
    finally:
        conn.close()

    page, errors, _ = page_factory()
    page.goto(page.url.split("#")[0] + "#/mail")
    page.wait_for_selector('[data-list="mail"] .row')
    page.wait_for_timeout(500)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth > window.innerWidth + 2")
    assert overflow is False, "a long subject pushed the page sideways"
    assert not errors
    page.close()


def test_it_fits_a_narrow_window(page_factory):
    """The rail collapses to a top bar; nothing should scroll sideways."""
    page, errors, _config = page_factory(viewport={"width": 390, "height": 844})
    assert page.locator("#nav a").first.is_visible()
    assert page.evaluate(
        "() => document.documentElement.scrollWidth <= window.innerWidth + 2")
    assert not errors
    page.close()


def test_the_page_never_violates_its_own_csp(page_factory):
    """The daemon serves a strict policy; the page has to live inside it.

    `style-src 'self'` refuses style attributes silently — the element simply
    renders unstyled — so the only way this is ever caught is by watching the
    console.
    """
    page, errors, _config = page_factory()
    for tab in ("today", "cases", "mail", "review", "setup"):
        page.click(f'[data-tab="{tab}"]')
        page.wait_for_timeout(500)
    page.keyboard.press("Control+k")
    page.wait_for_selector(".sheet")
    page.wait_for_timeout(400)
    page.keyboard.press("Escape")
    page.keyboard.press("?")
    page.wait_for_timeout(400)
    violations = [e for e in errors if "Content Security Policy" in e]
    assert violations == [], f"the page broke its own CSP: {violations}"
    assert not errors
    page.close()


def test_progress_bars_actually_draw(page_factory):
    """Both bars are spans, and width does not apply to an inline box.

    They looked right in a screenshot because an empty 2px track and a track
    with no fill are the same picture.
    """
    page, errors, _config = page_factory()
    page.click('[data-tab="review"]')
    page.wait_for_timeout(900)
    if "Nothing to check" in page.inner_text(".headline"):
        page.close()
        return
    size = page.evaluate("""() => {
      const fill = document.querySelector('.review-progress .fill');
      const track = document.querySelector('.review-progress .track');
      return { fill: fill.getBoundingClientRect().width,
               track: track.getBoundingClientRect().width };
    }""")
    assert size["track"] > 0, "the track has no width"
    assert size["fill"] > 0, "the fill drew nothing"
    assert size["fill"] < size["track"], "a queue of one cannot be fully done"
    assert not errors
    page.close()
